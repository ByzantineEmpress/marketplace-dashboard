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


def _cred(credentials, key, default=None):
    """Pull one credential out of a per-user credentials mapping.

    ``credentials`` is what :meth:`MarketplaceAdapter.credentials_for` returns:
    a plain dict of the current user's keys, or ``None`` when nothing was
    resolved. Every adapter read is written as

        _cred(credentials, "client_id") or config.EBAY_CLIENT_ID or ...

    so the user's own value wins and the instance-wide setting stays the
    fallback. That ordering is what stops one user's sync from using another
    user's account, while leaving an existing .env deployment working.
    """
    if not credentials:
        return default
    value = credentials.get(key)
    return value if value else default


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

    # Prefix of a generated placeholder title (e.g. "eBay listing 800657...") for
    # platforms whose listing sync cannot supply real titles. A sale carries the
    # real title, so record_sales() replaces a placeholder when it sees one.
    TITLE_PLACEHOLDER_PREFIX = ""

    # Rate-limit: max requests per window (overridden per platform)
    MAX_REQUESTS = 100
    RATE_WINDOW_SECONDS = 60

    # Retry settings
    MAX_RETRIES = 3
    BASE_DELAY = 1.0  # base delay for exponential back-off in seconds

    # Set by list_listings() when it cannot proceed for a reason worth
    # reporting — for example a shop that could not be resolved, or a token
    # missing a required scope. sync_all() turns this into a visible error.
    #
    # It exists because returning an empty list for both "this seller has no
    # listings" and "we could not work out which shop to read" is what made an
    # Etsy misconfiguration look like an empty account for several rounds of
    # debugging.
    last_error = None

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

    # Instance-wide config attribute backing each (platform, credential_key).
    #
    # Needed because the names do not line up mechanically: the eBay credential
    # is "client_id" but the config attribute is EBAY_CLIENT_ID. Guessing at
    # prefixes produced a silent None, so the mapping is explicit.
    CONFIG_FALLBACK = {
        ("ebay", "client_id"): "EBAY_CLIENT_ID",
        ("ebay", "client_secret"): "EBAY_CLIENT_SECRET",
        # eBay's authorize URL takes the RuName, not the callback URL.
        ("ebay", "ru_name"): "EBAY_RUNAME",
        ("etsy", "api_key"): "ETSY_API_KEY",
        ("etsy", "api_secret"): "ETSY_API_SECRET",
        ("poshmark", "username"): "POSHMARK_USERNAME",
        ("poshmark", "api_key"): "POSHMARK_API_KEY",
        ("amazon", "seller_id"): "AMAZON_SELLER_ID",
        ("amazon", "client_id"): "AMAZON_CLIENT_ID",
        ("amazon", "client_secret"): "AMAZON_CLIENT_SECRET",
        ("amazon", "refresh_token"): "AMAZON_REFRESH_TOKEN",
    }

    def credentials_for(self, db, user_id, keys, platform=None):
        """Resolve this adapter's API credentials for one user.

        Credentials are per-user: each seller generates their own keys in their
        own marketplace developer account. They live in
        ``user_marketplace_credentials``, encrypted at rest.

        Instance-wide values from ``config`` are used as a fallback so a
        deployment that still has credentials in ``.env`` keeps working, and so
        the local admin does not have to re-enter what is already configured.

        Returns a dict with one entry per requested key, ``None`` where nothing
        is configured.
        """
        platform_name = platform or self.PLATFORM
        resolved = {}

        rows = {}
        if db is not None and user_id is not None:
            from src.models import UserMarketplaceCredential

            for row in (
                db.query(UserMarketplaceCredential)
                .filter(
                    UserMarketplaceCredential.user_id == user_id,
                    UserMarketplaceCredential.platform == platform_name,
                )
                .all()
            ):
                rows[row.credential_key] = row.value

        for key in keys:
            if rows.get(key):
                resolved[key] = rows[key]
                continue
            # Fall back to the instance-wide setting. The names do not line up
            # mechanically (credential "client_id" vs config EBAY_CLIENT_ID), so
            # an explicit mapping is used rather than guessing at prefixes.
            config_name = self.CONFIG_FALLBACK.get((platform_name, key))
            resolved[key] = (getattr(config, config_name, None) or None) if config_name else None

        return resolved

    # ---------- Token management ----------

    def get_token(self, db: SessionLocal, user_id=None, credentials: dict = None) -> Optional[Dict[str, Any]]:
        """Get the stored OAuth token data for this platform and user.

        Returns ``None`` if this user has not connected the platform.

        ``user_id`` is required in practice; it is optional only so existing
        callers keep working during the transition. Passing ``None`` looks at
        rows with no owner, which will not match anything the application
        creates.

        ``credentials`` is forwarded to :meth:`refresh_token`, because a refresh
        needs the same per-user API key the original authorisation used.
        """
        account = self._find_account(db, user_id)
        if not account or not account.is_connected:
            return None

        # Check if the token is expired
        if account.token_expires_at and datetime.utcnow() >= account.token_expires_at:
            # Token expired — try to refresh.
            #
            # The user's credentials go with the refresh: the call needs the
            # same API key the original authorisation used (Etsy signs it with
            # the user's own keystring). The base signature accepts the keyword
            # so subclasses can opt in by declaring it.
            refresh_result = self.refresh_token(
                db, account, credentials=credentials, user_id=user_id
            )
            if not refresh_result.get("success"):
                return None
            # Reload account after refresh
            account = self._find_account(db, user_id)

        token_data = (account.token_data or {})
        return {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            **token_data,
        }

    def _find_account(self, db, user_id):
        """The account row for this platform owned by ``user_id``."""
        query = db.query(MarketplaceAccount).filter(
            MarketplaceAccount.platform == self.PLATFORM
        )
        if user_id is None:
            query = query.filter(MarketplaceAccount.user_id.is_(None))
        else:
            query = query.filter(MarketplaceAccount.user_id == user_id)
        return query.first()

    def refresh_token(self, db: SessionLocal, account: MarketplaceAccount,
                      credentials: dict = None, user_id=None) -> Dict[str, Any]:
        """Refresh an expired access token using the stored refresh token.

        Sub-classes MAY override this; the default is to return failure.
        ``credentials`` carries the owning user's settings, since a refresh is
        signed with the same key as the original authorisation.
        """
        return {"success": False, "error": "Refresh not implemented for this platform"}

    def store_tokens(self, db: SessionLocal, access_token: str,
                     refresh_token: str = None,
                     token_expires_in: int = None,
                     extra_data: dict = None,
                     shop_id: str = None,
                     shop_name: str = None,
                     user_id=None) -> MarketplaceAccount:
        """Store OAuth tokens for this platform and user.

        Creates or updates the marketplace account record **for this user**.
        Scoping matters: without it, two users connecting the same marketplace
        would overwrite each other's tokens.
        """
        account = self._find_account(db, user_id)

        if not account:
            account = MarketplaceAccount(platform=self.PLATFORM, user_id=user_id)

        account.access_token = access_token
        # Only overwrite the refresh token when a real one is supplied. Several
        # callers legitimately pass None (they have no new refresh token), and
        # assigning unconditionally wiped the stored one — which silently turned
        # a refreshable connection into one that dies when the access token
        # expires, with no way back except a manual reconnect.
        if refresh_token:
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
                       team_id: Optional[int] = None,
                       owner_user_id: Optional[int] = None) -> Dict[str, int]:
        """Store (or update) listings in the database.

        Returns a summary: ``{"added": N, "updated": M, "failed": K}``
        """
        added = 0
        updated = 0
        failed = 0
        first_error = None

        # Resolve the destination team.
        #
        # This MUST come from the syncing user. It previously fell back to
        # "the first team in the database", which with more than one account
        # writes one tenant's listings into another's workspace: a real sync
        # put 500 of one seller's listings into the local admin's team.
        target_team_id = team_id

        if not target_team_id and owner_user_id is not None:
            from src.models import TeamMembership
            membership = (
                db.query(TeamMembership)
                .filter(TeamMembership.user_id == owner_user_id)
                .order_by(TeamMembership.id)
                .first()
            )
            if membership:
                target_team_id = membership.team_id

        if not target_team_id:
            # Refuse rather than guess. Writing to whichever team happens to be
            # first is worse than storing nothing.
            self.last_error = (
                "Could not determine which team these listings belong to. "
                "This account is not a member of any team yet."
            )
            return {"added": 0, "updated": 0, "failed": 0, "first_error": self.last_error}

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

            except Exception as exc:
                failed += 1
                if first_error is None:
                    # Keep the first failure verbatim. A bare "failed += 1" made
                    # a total insert failure look exactly like an empty account,
                    # which is how a normalisation bug stayed hidden while every
                    # fetched listing was rejected.
                    first_error = f"{type(exc).__name__}: {exc}"

        db.commit()
        return {"added": added, "updated": updated, "failed": failed,
                "first_error": first_error}

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

    # ---------- Sales (completed orders) ----------

    def _resolve_team_id(self, db: SessionLocal, owner_user_id):
        """The team a synced row belongs to, taken from the user who ran the sync.

        Mirrors store_listings: the destination must come from the syncing user,
        never from "the first team in the database", which would write one
        tenant's sales into another's workspace.
        """
        if owner_user_id is None:
            return None
        from src.models import TeamMembership

        membership = (
            db.query(TeamMembership)
            .filter(TeamMembership.user_id == owner_user_id)
            .order_by(TeamMembership.id)
            .first()
        )
        return membership.team_id if membership else None

    def record_sales(self, db: SessionLocal, sales: List[Dict[str, Any]],
                     owner_user_id=None) -> Dict[str, int]:
        """Apply completed sales to the listings table.

        A sale whose item we already track is marked sold with the REAL sale price
        and date. That is strictly better than the manual flow, which records the
        moment the button was pressed and takes the price on trust.

        A sale whose item we have NO row for still becomes a listing. An item
        leaves the active-listing endpoints the moment it sells, so anything
        bought and sold between two syncs would otherwise never be seen at all,
        and its revenue would be missing from every total.
        """
        recorded = 0
        created = 0
        team_id = self._resolve_team_id(db, owner_user_id)

        for sale in sales:
            item_id = str(sale.get("platform_listing_id") or "")
            if not item_id:
                continue
            try:
                existing = (
                    db.query(Listing)
                    .filter_by(platform=self.PLATFORM, platform_listing_id=item_id)
                    .first()
                )
                sold_at = sale.get("sold_at") or datetime.utcnow()
                price_cents = int(sale.get("price_cents") or 0)
                currency = sale.get("currency") or "CAD"
                title = sale.get("title") or ""
                image_url = sale.get("image_url") or ""
                images = sale.get("images") or ([image_url] if image_url else [])

                if existing:
                    existing.is_sold = True
                    existing.status = "sold"
                    existing.sold_at = sold_at
                    existing.available_quantity = 0
                    if price_cents:
                        existing.price_cents = price_cents
                        existing.price_raw = f"{currency} {price_cents / 100:.2f}"
                        existing.currency = currency
                    if title and (
                        not existing.title
                        or (
                            self.TITLE_PLACEHOLDER_PREFIX
                            and existing.title.startswith(self.TITLE_PLACEHOLDER_PREFIX)
                        )
                    ):
                        existing.title = title
                    # Only fill a missing picture; never clobber one the user set.
                    if image_url and not existing.image_url:
                        existing.image_url = image_url
                        existing.images_json = images
                    existing.updated_at = datetime.utcnow()
                    recorded += 1
                else:
                    db.add(Listing(
                        platform=self.PLATFORM,
                        platform_listing_id=item_id,
                        title=title or f"{self.PLATFORM} item {item_id}",
                        price_cents=price_cents,
                        price_raw=f"{currency} {price_cents / 100:.2f}",
                        currency=currency,
                        image_url=image_url or None,
                        images_json=images or None,
                        status="sold",
                        is_sold=True,
                        sold_at=sold_at,
                        available_quantity=0,
                        team_id=team_id,
                    ))
                    created += 1
            except Exception:
                # One bad sale must not abandon the rest of the batch.
                db.rollback()
                continue

        db.commit()
        return {"recorded": recorded, "created": created}

    def sync_sales(self, db: SessionLocal, user_id=None,
                   credentials: dict = None) -> Dict[str, Any]:
        """Pull completed sales for this platform.

        Platforms with no sales API inherit this no-op and report
        ``supported: False``, so the sync route can call it unconditionally
        rather than branching per platform.
        """
        return {"supported": False, "success": True, "fetched": 0,
                "recorded": 0, "created": 0}

    # ---------- Sync orchestration ----------

    def sync_all(self, db: SessionLocal, user_id=None, credentials: dict = None) -> Dict[str, Any]:
        """Full sync: fetch listings, store them, return a summary.

        This is the main entry-point called from the API routes.

        ``user_id`` scopes the whole operation: the stored token belongs to that
        user, and ``credentials`` are their own marketplace API keys. Without it
        a sync would read whichever account happened to be in the table first.
        """
        result = {
            "success": False,
            "platform": self.PLATFORM,
            "listings_fetched": 0,
            "listings_added": 0,
            "listings_updated": 0,
            "errors": [],
        }

        # Reset per run so a previous failure is not reported again.
        self.last_error = None

        try:
            # Check we have a valid token for this user
            token = self.get_token(db, user_id=user_id, credentials=credentials)
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
            listings = self.list_listings(db=db, user_id=user_id, credentials=credentials)
            result["listings_fetched"] = len(listings)

            if not listings:
                # Distinguish "this account has nothing" from "we could not
                # work out where to look". Reporting both as a silent zero is
                # what hid a shop-resolution bug behind "Synced 0 listing(s)".
                if self.last_error:
                    result["errors"].append(self.last_error)
                    return result
                result["success"] = True
                result["note"] = "No listings found"
                return result

            # Store in DB, owned by the team of the user who ran the sync.
            stored = self.store_listings(db, listings, owner_user_id=user_id)
            result["listings_added"] = stored["added"]
            result["listings_updated"] = stored["updated"]
            result["listings_failed"] = stored.get("failed", 0)
            result["success"] = True

            # Partial or total insert failure must be visible. Reporting
            # "0 added" alongside "500 fetched" with no error is what let a
            # schema mismatch go unnoticed.
            if stored.get("failed"):
                result["errors"].append(
                    f"{stored['failed']} of {len(listings)} listing(s) could not be "
                    f"stored — first error: {stored.get('first_error')}"
                )

        except Exception as e:
            result["errors"].append(str(e))

        return result
