"""eBay Selling API v2 adapter.

Implements the MarketplaceAdapter ABC for eBay's modern REST APIs.

Auth: OAuth 2.0 with the eBay Selling API.

The authorize request needs the user's RuName (eBay "redirect URL name") as
``redirect_uri`` -- NOT the callback URL. eBay validates the RuName and uses the
"Auth Accepted URL" configured against it to send the user back. Passing a URL
here makes eBay answer with a generic ``temporarily_unavailable`` 500.

Endpoints used:
- GET /sell/inventory/v1/inventory_item — list your inventory items
- GET /sell/marketplace_listing/v1/marketplace_listing — list active listings

References:
- https://developer.ebay.com/api-docs/sell/inventory/resources/inventory_item/methods
- https://developer.ebay.com/api-docs/sell/marketplace_listing/resources/marketplace_listing/methods
- https://edp.ebay.com/support/knowledge-base/5075  (Quick OAuth Guide)

NOTE: This uses the eBay Selling API v2 (the current, supported API).
The old v1 Selling API is deprecated.
"""

import os
from typing import Any, Dict, List, Optional

import httpx

from src.adapters.base import MarketplaceAdapter, _cred
from src.database import SessionLocal
from src.config import config

# eBay API base URL (production)
EBAY_API_BASE = "https://api.ebay.com"

# eBay OAuth token endpoint
EBAY_OAUTH_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

# eBay OAuth authorisation URL
EBAY_AUTH_URL = "https://auth.ebay.com/oauth2/authorize"

# Supported marketplaces
EBAY_MARKETPLACES = ["EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_FR", "EBAY_IT", "EBAY_CA", "EBAY_AU"]

# Inventory item types we care about
INVENTORY_ITEM_TYPE = "SingleOffer"


class eBayAdapter(MarketplaceAdapter):
    """Adapter for eBay's Selling API v2."""

    PLATFORM = "ebay"
    PLATFORM_LABEL = "eBay"
    MAX_REQUESTS = 50  # eBay v2 allows ~50 req/min
    RATE_WINDOW_SECONDS = 60

    def get_platform_name(self) -> str:
        return "eBay"

    def get_platform_icon(self) -> str:
        # Simple SVG-based eBay logo
        return '<svg viewBox="0 0 60 30" width="50" height="25"><text x="5" y="22" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#E53238">ebay</text></svg>'

    def _ru_name(self, credentials: dict) -> str:
        """The eBay RuName ("redirect URL name") for this user's app.

        eBay does NOT accept the callback URL as ``redirect_uri``. Its OAuth
        authorize endpoint requires the RuName, and the callback URL is instead
        configured *inside* the RuName in the eBay developer portal as the
        "Auth Accepted URL". Passing the URL produces a generic
        ``temporarily_unavailable`` 500 from eBay, which is what this used to do.

        Reference (eBay KB, Quick OAuth Guide):
            redirectUri - OAuth Enabled RuName for the clientId
            redirectUrl - Auth Accepted URL associated with the redirectUri
        """
        return (
            _cred(credentials, "ru_name")
            or config.EBAY_RUNAME
            or os.environ.get("EBAY_RUNAME")
            or ""
        ).strip()

    def get_authorization_url(self, state: str = "", credentials: dict = None) -> str:
        """Build the eBay OAuth 2.0 authorisation URL.

        ``credentials`` carries this user's own client ID and RuName. Both the
        client id and the RuName come from the user's own eBay application, so
        one seller's connection is never authorised against another's app.
        """
        from urllib.parse import urlencode

        client_id = _cred(credentials, "client_id") or config.EBAY_CLIENT_ID or os.environ.get("EBAY_CLIENT_ID") or os.environ.get("EBUY_CLIENT_ID") or ""
        ru_name = self._ru_name(credentials)

        if not ru_name:
            # Caught before the user is sent to eBay, because eBay's own error
            # for this ("temporarily_unavailable") gives no hint about the cause.
            raise ValueError(
                "eBay needs your RuName (the 'redirect URL name' from your eBay "
                "app's User Tokens page). Save it, then press Connect."
            )

        # eBay scope URNs. The previous values
        # (".../auth/oauth/sell_inventory_readonly") were not real scopes and
        # eBay rejected the whole request. Correct form is
        # "https://api.ebay.com/oauth/api_scope/<name>".
        #
        # Only what this adapter actually calls is requested: the Inventory API
        # and the Marketplace Listing API, both covered by sell.inventory.readonly.
        scopes = [
            "https://api.ebay.com/oauth/api_scope",
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
        ]

        params = {
            "client_id": client_id,
            # The RuName, NOT the callback URL.
            "redirect_uri": ru_name,
            "response_type": "code",
            # eBay wants the scope list space-separated, then URL-encoded.
            # urlencode renders the spaces as "+", which is that encoding.
            "scope": " ".join(scopes),
            "state": state,
        }

        return f"{EBAY_AUTH_URL}?{urlencode(params)}"

    def handle_callback(self, code: str, state: str = "", credentials: dict = None,
                        user_id=None, code_verifier: str = None,
                        seller_id: str = None) -> Dict[str, Any]:
        """Exchange an authorisation code for access and refresh tokens.

        POSTs to eBay's OAuth token endpoint and stores the tokens. The token
        request must repeat the same ``redirect_uri`` used to authorise, which
        for eBay is the RuName.

        ``code_verifier`` and ``seller_id`` are accepted and ignored: the shared
        callback route passes them for every platform, and eBay uses neither
        (it is a confidential client with a client secret, not PKCE).
        """
        import base64

        ru_name = self._ru_name(credentials)
        client_id = _cred(credentials, "client_id") or config.EBAY_CLIENT_ID or os.environ.get("EBAY_CLIENT_ID") or os.environ.get("EBUY_CLIENT_ID") or ""
        client_secret = _cred(credentials, "client_secret") or config.EBAY_CLIENT_SECRET or os.environ.get("EBAY_CLIENT_SECRET") or os.environ.get("EBUY_CLIENT_SECRET") or ""

        if not ru_name:
            return {"success": False,
                    "error": "eBay RuName is not saved, so the token exchange "
                             "cannot verify the redirect. Save it and reconnect."}

        # eBay expects Basic Auth with client_id:client_secret.
        # Named basic_auth, not "credentials": that name is the parameter, and
        # reusing it here would shadow the caller's per-user values.
        basic_auth = f"{client_id}:{client_secret}"
        auth_header = base64.b64encode(basic_auth.encode()).decode()

        try:
            resp = httpx.post(
                EBAY_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": ru_name,
                },
                headers={
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )
            resp.raise_for_status()
            token_data = resp.json()

            # Store the tokens
            db = SessionLocal()
            try:
                self.store_tokens(
                    db,
                    access_token=token_data["access_token"],
                    refresh_token=token_data.get("refresh_token", ""),
                    token_expires_in=token_data.get("expires_in", 7200),
                    extra_data=token_data,
            user_id=user_id,
                )
                return {"success": True, "token_data": token_data}
            finally:
                db.close()

        except httpx.HTTPError as e:
            return {"success": False, "error": f"OAuth error: {e}"}

    # -- Token refresh --

    def refresh_token(self, db: SessionLocal, account, credentials: dict = None,
                      user_id=None) -> Dict[str, Any]:
        """Refresh an expired access token using the stored refresh token.

        ``credentials`` holds the owning user's own eBay keys, so one user's
        refresh never signs with another user's application.
        """
        import base64
        import os

        client_id = _cred(credentials, "client_id") or config.EBAY_CLIENT_ID or os.environ.get("EBAY_CLIENT_ID") or os.environ.get("EBUY_CLIENT_ID") or ""
        client_secret = _cred(credentials, "client_secret") or config.EBAY_CLIENT_SECRET or os.environ.get("EBAY_CLIENT_SECRET") or os.environ.get("EBUY_CLIENT_SECRET") or ""
        # Named basic_auth, not "credentials": that name is the parameter, and
        # reusing it here would shadow the caller's per-user values.
        basic_auth = f"{client_id}:{client_secret}"
        auth_header = base64.b64encode(basic_auth.encode()).decode()

        try:
            resp = httpx.post(
                EBAY_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": account.refresh_token,
                },
                headers={
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )
            resp.raise_for_status()
            token_data = resp.json()

            # Update stored tokens
            self.store_tokens(
                db,
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token", account.refresh_token),
                token_expires_in=token_data.get("expires_in", 7200),
                extra_data=token_data,
                shop_id=account.shop_id,
                shop_name=account.shop_name,
            user_id=user_id,
            )
            return {"success": True, "token_data": token_data}
        except httpx.HTTPError as e:
            return {"success": False, "error": f"Token refresh failed: {e}"}

    # -- Listing fetching (Selling API v2) --

    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None,
                      user_id=None, credentials: dict = None) -> List[Dict[str, Any]]:
        """Fetch active eBay listings via the Selling API v2.

        Uses:
        - Inventory API to get all inventory items
        - Marketplace Listing API for listing details

        Both are scoped by the seller's own access token, so unlike Etsy's
        public feed they can only ever return this seller's data. The thing to
        get right here is the marketplace: an item on EBAY_CA will not be found
        by querying EBAY_US, and hardcoding EBAY_US silently drops every
        non-US listing.
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            token = self.get_token(db, user_id=user_id, credentials=credentials)
            if not token:
                return []

            headers = {
                "Authorization": f"Bearer {token['access_token']}",
                "Content-Type": "application/json",
            }

            # Collect inventory items first. Each is tagged with the marketplace
            # it was found in so listing details are fetched from that same
            # marketplace, not a hardcoded EBAY_US.
            inventory_items = self._fetch_inventory_items(headers, max_results)
            if not inventory_items:
                return []

            all_listings = []
            for marketplace, item in inventory_items:
                listing = self._get_listing_details(item, headers, marketplace)
                if listing:
                    all_listings.append(listing)

                if len(all_listings) >= max_results:
                    break

            return all_listings
        finally:
            if close_db:
                db.close()

    def _fetch_inventory_items(self, headers: dict, limit: int) -> List[tuple]:
        """Fetch inventory items from eBay's Inventory API.

        Endpoint: GET /sell/inventory/v1/inventory_item

        Returns ``(marketplace_id, item)`` pairs. The marketplace is preserved
        because the Marketplace Listing API needs it to find the item's live
        listing, and a seller can list on several marketplaces at once.
        """
        items = []

        for marketplace in EBAY_MARKETPLACES:
            page = 1
            page_size = 250  # eBay max per page

            while len(items) < limit:
                params = {
                    "page_number": page,
                    "page_size": min(page_size, limit - len(items)),
                    "type": INVENTORY_ITEM_TYPE,
                    "filter": '{"statuses":["ACTIVE"]}',
                    "marketplace_id": marketplace,
                }

                try:
                    resp = httpx.get(
                        f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item",
                        headers=headers,
                        params=params,
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    inventory_items = data.get("inventoryItems", [])
                    if not inventory_items:
                        break

                    items.extend((marketplace, it) for it in inventory_items)

                    if len(inventory_items) < page_size:
                        break  # Last page
                    page += 1

                except httpx.HTTPError as exc:
                    self.last_error = f"eBay inventory fetch failed: {exc}"
                    break

                if len(items) >= limit:
                    break

        return items[:limit]

    def _get_listing_details(self, item: dict, headers: dict, marketplace: str) -> Optional[dict]:
        """Get listing details from the Marketplace Listing API.

        Uses the marketplace the inventory item came from; hardcoding EBAY_US
        here would silently drop every listing on EBAY_CA, EBAY_GB, etc.
        """
        try:
            listing_id = item.get("id")
            if not listing_id:
                return None

            # Try to get from Marketplace Listing API
            try:
                resp = httpx.get(
                    f"{EBAY_API_BASE}/sell/marketplace_listing/v1/marketplace_listing",
                    headers=headers,
                    params={
                        "marketplace_id": marketplace,
                        "filter": '{"statuses":["ACTIVE"]}',
                        "page_size": 50,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for ml in data.get("marketplaceListings", []):
                        if ml.get("inventoryItemId") == listing_id:
                            return self._normalise_listing(ml)
            except httpx.HTTPError as exc:
                self.last_error = f"eBay listing-detail fetch failed: {exc}"

            # Fall back to inventory item data
            return self._normalise_inventory_item(item)

        except (KeyError, IndexError, TypeError):
            return None

    def _normalise_listing(self, item: dict) -> Optional[dict]:
        """Normalise a Marketplace Listing API response."""
        try:
            title = item.get("title", "")
            primary_price = item.get("primaryPrice", {})
            price = primary_price.get("value", 0) or 0
            currency = primary_price.get("currency", "USD")

            images = []
            media = item.get("listingMedia", {})
            for url in media.get("imageUrls", []):
                images.append(url)

            categories = []
            for cat in item.get("categories", []):
                categories.append(cat.get("name", ""))

            quantity = item.get("quantityAvailable", 0)
            status = item.get("status", "UNKNOWN")

            return {
                "platform_listing_id": item.get("id"),
                "title": title,
                "description": item.get("description", ""),
                "price_raw": f"{currency} {price}",
                "price_cents": int(float(price) * 100),
                "currency": currency,
                "status": "active" if status == "ACTIVE" else status.lower(),
                "is_sold": status not in ("ACTIVE", "REVISE"),
                "image_url": images[0] if images else "",
                "images_json": images,
                "category": " / ".join(categories),
                "sku": item.get("sku"),
                "available_quantity": quantity,
                "inventory_item_id": item.get("inventoryItemId"),
                "original_url": f"https://www.ebay.com/itm/{item.get('id', '')}",
                "platform": self.PLATFORM,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def _normalise_inventory_item(self, item: dict) -> Optional[dict]:
        """Normalise an inventory item (fallback when listing data unavailable)."""
        try:
            title = item.get("title", "")
            price = item.get("price", {})
            price_amount = price.get("value", 0) or 0
            currency = price.get("currency", "USD")

            images = []
            for img in item.get("product", {}).get("productImages", []):
                images.append(img.get("url", ""))

            category = ""
            for cat in item.get("product", {}).get("productDimensions", {}).get("itemDimensions", []):
                if cat.get("name") == "Category":
                    category = cat.get("value", "")

            return {
                "platform_listing_id": item.get("id"),
                "title": title or f"eBay Item {item.get('id', '')}",
                "description": item.get("product", {}).get("productDescription", ""),
                "price_raw": f"{currency} {price_amount}",
                "price_cents": int(float(price_amount) * 100),
                "currency": currency,
                "status": "active",
                "is_sold": False,
                "image_url": images[0] if images else "",
                "images_json": images,
                "category": category,
                "sku": item.get("sku"),
                "available_quantity": item.get("quantityAvailable", 0),
                "platform": self.PLATFORM,
            }
        except (KeyError, IndexError, TypeError):
            return None
