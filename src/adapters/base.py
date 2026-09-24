"""Base adapter class — the interface every marketplace plugin must implement.

The base class handles:
- OAuth token management (storage and automatic refresh).
- Rate-limit tracking so we don't hammer the API.
- Retry logic with exponential back-off on transient errors.

Sub-classes only need to implement the platform-specific methods.
"""

import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from src.database import SessionLocal
from src.models import Listing, MarketplaceAccount
from src.config import config


class MarketplaceAdapter(ABC):
    """Abstract base for all marketplace API adapters.

    Subclasses MUST implement:
    - ``get_authorization_url()`` — returns the OAuth URL for user auth.
    - ``handle_callback()`` — process the OAuth callback and store tokens.
    - ``list_listings()`` — fetch listings from the platform.
    - ``get_platform_name()`` — returns the human-readable name.
    - ``get_platform_icon()`` — returns the SVG/CSS class for the logo.
    """

    PLATFORM = None  # must be overridden, e.g. "ebay" or "etsy"
    PLATFORM_LABEL = None  # e.g. "eBay" or "Etsy"

    # Rate-limit: max requests per window (overridden per platform)
    MAX_REQUESTS = 100
    RATE_WINDOW_SECONDS = 60

    # Retry settings
    MAX_RETRIES = 3
    BASE_DELAY = 1.0  # base delay for exponential back-off in seconds

    # ---------- OAuth (abstract — must be implemented) ----------

    @abstractmethod
    def get_authorization_url(self, state: str = "") -> str:
        """Return the OAuth authorisation URL the user visits to grant access."""
        ...

    @abstractmethod
    def handle_callback(self, code: str, state: str = "") -> Dict[str, Any]:
        """Exchange an auth code for tokens and store them in the DB.

        Returns a dict with keys: ``"success" (bool)``, ``"error" (str, optional)``.
        """
        ...

    # ---------- Listing fetching (abstract — must be implemented) ----------

    @abstractmethod
    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None) -> List[Dict[str, Any]]:
        """Fetch active listings from the platform.

        Returns a list of plain dicts ready to be stored or displayed.
        Keys must match the fields in Listing model.
        """
        ...

    @abstractmethod
    def get_platform_name(self) -> str:
        """Return the human-readable platform name."""
        ...

    @abstractmethod
    def get_platform_icon(self) -> str:
        """Return a CSS class or SVG path for the platform logo."""
        ...

    # ---------- Token management ----------

    def get_token(self, db: SessionLocal) -> Optional[Dict[str, Any]]:
        """Get the stored OAuth token data for this platform.

        Returns ``None`` if the platform is not connected,
        or the token dict if it is.
        """
        account = db.query(MarketplaceAccount).filter_by(platform=self.PLATFORM).first()
        if not account or not account.is_connected:
            return None

        # Check if the token is expired
        if account.token_expires_at and datetime.utcnow() >= account.token_expires_at:
            # Token expired — try to refresh
            refresh_result = self.refresh_token(db, account)
            if not refresh_result.get("success"):
                return None
            # Reload account after refresh
            account = db.query(MarketplaceAccount).filter_by(platform=self.PLATFORM).first()

        token_data = (account.token_data or {})
        return {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            **token_data,
        }

    def refresh_token(self, db: SessionLocal, account: MarketplaceAccount) -> Dict[str, Any]:
        """Refresh an expired access token using the stored refresh token.

        Sub-classes MAY override this; the default is to return failure.
        """
        return {"success": False, "error": "Refresh not implemented for this platform"}

    def store_tokens(self, db: SessionLocal, access_token: str,
                     refresh_token: str = None,
                     token_expires_in: int = None,
                     extra_data: dict = None,
                     shop_id: str = None,
                     shop_name: str = None) -> MarketplaceAccount:
        """Store OAuth tokens for this platform in the database.

        Creates or updates the marketplace account record.
        """
        account = db.query(MarketplaceAccount).filter_by(platform=self.PLATFORM).first()

        if not account:
            account = MarketplaceAccount(platform=self.PLATFORM)

        account.access_token = access_token
        account.refresh_token = refresh_token

        if token_expires_in:
            account.token_expires_at = datetime.utcnow() + timedelta(seconds=token_expires_in)
        else:
            account.token_expires_at = datetime.utcnow() + timedelta(hours=1)  # default 1 hour

        if extra_data:
            account.token_data = extra_data

        if shop_id:
            account.shop_id = shop_id
        if shop_name:
            account.shop_name = shop_name

        account.is_connected = True
        account.last_synced = datetime.utcnow()

        db.add(account)
        db.commit()
        db.refresh(account)
        return account

    # ---------- Listing storage ----------

    def store_listings(self, db: SessionLocal,
                       listings: List[Dict[str, Any]],
                       team_id: Optional[int] = None) -> Dict[str, int]:
        """Store (or update) listings in the database.

        Returns a summary: ``{"added": N, "updated": M, "failed": K}``
        """
        added = 0
        updated = 0
        failed = 0

        # Resolve fallback team_id if not explicitly provided
        target_team_id = team_id
        if not target_team_id:
            from src.models import Team
            default_t = db.query(Team).first()
            if default_t:
                target_team_id = default_t.id

        for data in listings:
            try:
                # Check if listing already exists
                existing = db.query(Listing).filter_by(
                    platform=self.PLATFORM,
                    platform_listing_id=str(data.get("platform_listing_id", ""))
                ).first()

                if existing:
                    # Update existing listing
                    for key, value in data.items():
                        setattr(existing, key, value)
                    if not existing.team_id and target_team_id:
                        existing.team_id = target_team_id
                    existing.updated_at = datetime.utcnow()
                    existing.last_fetched = datetime.utcnow()
                    updated += 1
                else:
                    # Create new listing. Both adapters include "platform"
                    # in their dicts, so only pass it explicitly if absent
                    # (passing it twice raises TypeError).
                    data = dict(data)
                    data.setdefault("platform", self.PLATFORM)
                    new_listing = Listing(**data)
                    if not getattr(new_listing, "team_id", None) and target_team_id:
                        new_listing.team_id = target_team_id
                    new_listing.created_at = datetime.utcnow()
                    new_listing.updated_at = datetime.utcnow()
                    new_listing.last_fetched = datetime.utcnow()
                    db.add(new_listing)
                    added += 1

            except Exception:
                failed += 1

        db.commit()
        return {"added": added, "updated": updated, "failed": failed}

    # ---------- Rate limiting ----------

    def __init__(self):
        self._request_times: list = []  # timestamps of recent requests

    def _check_rate_limit(self) -> bool:
        """Check if we've exceeded the rate limit. Returns True if OK to proceed."""
        now = time.time()
        # Remove old requests outside the window
        self._request_times = [t for t in self._request_times if now - t < self.RATE_WINDOW_SECONDS]

        if len(self._request_times) >= self.MAX_REQUESTS:
            return False  # rate limited
        return True

    def _record_request(self):
        """Record a request timestamp for rate limiting."""
        self._request_times.append(time.time())

    # ---------- Retry wrapper ----------

    def _retry_on_failure(self, func, *args, **kwargs):
        """Run *func* with exponential back-off retries on failure.

        Retries up to ``self.MAX_RETRIES`` times, doubling the delay each time.
        """
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as e:
                last_error = e
                delay = self.BASE_DELAY * (2 ** attempt)
                time.sleep(delay)

        raise last_error

    # ---------- Sync orchestration ----------

    def sync_all(self, db: SessionLocal) -> Dict[str, Any]:
        """Full sync: fetch listings, store them, return a summary.

        This is the main entry-point called from the API routes.
        """
        result = {
            "success": False,
            "platform": self.PLATFORM,
            "listings_fetched": 0,
            "listings_added": 0,
            "listings_updated": 0,
            "errors": [],
        }

        try:
            # Check we have a valid token
            token = self.get_token(db)
            if not token:
                result["errors"].append("No valid access token — connect the account first")
                return result

            # Check rate limit (sleep if needed)
            if not self._check_rate_limit():
                # Wait for the oldest request to fall out of the window
                oldest = min(self._request_times)
                wait_time = self.RATE_WINDOW_SECONDS - (time.time() - oldest)
                time.sleep(max(wait_time, 0.1))

            # Record this request
            self._record_request()

            # Fetch listings
            listings = self.list_listings(db=db)
            result["listings_fetched"] = len(listings)

            if not listings:
                result["success"] = True
                result["note"] = "No listings found"
                return result

            # Store in DB
            stored = self.store_listings(db, listings)
            result["listings_added"] = stored["added"]
            result["listings_updated"] = stored["updated"]
            result["success"] = True

        except Exception as e:
            result["errors"].append(str(e))

        return result
