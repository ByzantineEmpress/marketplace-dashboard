"""Etsy Open API v3 adapter.

Implements the MarketplaceAdapter ABC for Etsy's REST API.

Auth: OAuth 2.0 with Etsy's Seller App access.
Endpoints used:
- GET /v3/application/listings/active — get all active listings
- GET /v3/application/shops/{shop_id}/listings — get listings by shop
- GET /v3/application/listings/{listing_id} — get details for one listing

References:
- https://developers.etsy.com/documentation/apis/reference/shop-listing
"""

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from src.adapters.base import MarketplaceAdapter, _cred
from src.database import SessionLocal
from src.config import config

# Etsy API base URL
ETSY_API_BASE = "https://api.etsy.com/v3"

# Etsy OAuth token URL
ETSY_OAUTH_URL = "https://openapi.etsy.com/v3/public/oauth/token"

# Etsy OAuth authorisation URL
ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"


class EtsyAdapter(MarketplaceAdapter):
    """Adapter for Etsy's Open API v3."""

    PLATFORM = "etsy"
    PLATFORM_LABEL = "Etsy"
    MAX_REQUESTS = 25  # Etsy standard tier allows 25 req/min
    RATE_WINDOW_SECONDS = 60

    def get_platform_name(self) -> str:
        return "Etsy"

    def get_platform_icon(self) -> str:
        # Simple SVG-based Etsy logo
        return '<svg viewBox="0 0 60 30" width="50" height="25"><text x="3" y="22" font-family="Arial, sans-serif" font-size="20" font-weight="bold" fill="#F56400">Etsy</text></svg>'

    def get_authorization_url(self, state: str = "", credentials: dict = None,
                              code_challenge: str = "") -> str:
        """Build the Etsy OAuth 2.0 authorisation URL.

        Parameters:
            state: single-use CSRF state, echoed back by Etsy.
            credentials: this user's own keystring.
            code_challenge: the PKCE S256 challenge. REQUIRED by Etsy — the flow
                is rejected without it — so a missing one raises here rather
                than returning a URL that is certain to fail. That silent
                failure is what produced an unhelpful "no valid access token"
                message much later in the process.

        The user is redirected here to grant permissions; Etsy then redirects
        back to the callback with a code.
        """
        import os

        if not code_challenge:
            raise ValueError(
                "Etsy requires PKCE: get_authorization_url needs a code_challenge"
            )

        redirect_uri = os.environ.get("ETSY_REDIRECT_URI") or f"{config.APP_BASE_URL}/api/auth/etsy/callback"
        api_key = _cred(credentials, "api_key") or config.ETSY_API_KEY or os.environ.get("ETSY_API_KEY") or os.environ.get("ESY_API_KEY") or ""

        # Required scopes for reading listings
        scopes = "listings_r"

        params = {
            "response_type": "code",
            "client_id": api_key,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            # Etsy requires S256 explicitly; "plain" is not accepted.
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        # quote() matters here: the redirect_uri contains "://" and "/", and an
        # unencoded value was being sent before.
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
        return f"{ETSY_AUTH_URL}?{query}"

    def handle_callback(self, code: str, state: str = "", credentials: dict = None,
                        user_id=None, code_verifier: str = "") -> Dict[str, Any]:
        """Exchange an authorisation code for access and refresh tokens.

        Also stores the shop_id from the response.

        ``code_verifier`` is the PKCE verifier whose S256 challenge was sent with
        the authorisation request. Etsy requires it on the token request; without
        it the exchange fails with an unhelpful error.
        """
        import os

        redirect_uri = os.environ.get("ETSY_REDIRECT_URI") or f"{config.APP_BASE_URL}/api/auth/etsy/callback"
        api_key = _cred(credentials, "api_key") or config.ETSY_API_KEY or os.environ.get("ETSY_API_KEY") or os.environ.get("ESY_API_KEY") or ""
        api_secret = _cred(credentials, "api_secret") or config.ETSY_API_SECRET or os.environ.get("ETSY_API_SECRET") or os.environ.get("ESY_API_SECRET") or ""

        try:
            token_request = {
                "grant_type": "authorization_code",
                "client_id": api_key,
                "code": code,
                "redirect_uri": redirect_uri,
            }
            if code_verifier:
                token_request["code_verifier"] = code_verifier
            resp = httpx.post(
                ETSY_OAUTH_URL,
                data=token_request,
                # Etsy expects the credential pair rather than HTTP Basic.
                headers={"x-api-key": f"{api_key}:{api_secret}"} if api_key and api_secret else {},
                timeout=30,
            )
            resp.raise_for_status()
            token_data = resp.json()

            # Extract shop_id from token response
            shop_id = token_data.get("shop_id")

            db = SessionLocal()
            try:
                self.store_tokens(
                    db,
                    access_token=token_data["access_token"],
                    refresh_token=token_data.get("refresh_token", ""),
                    token_expires_in=token_data.get("expires_in", 7200),
                    extra_data=token_data,
                    shop_id=shop_id,
            user_id=user_id,
                )

                # Try to fetch shop name
                if shop_id:
                    try:
                        headers = {
                            "Authorization": f"Bearer {token_data['access_token']}",
                            "x-api-key": f"{api_key}:{api_secret}",
                        }
                        resp = httpx.get(
                            f"{ETSY_API_BASE}/applications/{api_key}/shops",
                            headers=headers,
                            timeout=15,
                        )
                        if resp.status_code == 200:
                            shops = resp.json()
                            shop_name = shops.get("results", [{}])[0].get("shop_name", "")
                            if shop_name:
                                self.store_tokens(
                                    db,
                                    access_token=token_data["access_token"],
                                    shop_name=shop_name,
            user_id=user_id,
                                )
                    except Exception:
                        pass  # Don't fail auth if shop name fetch fails

                return {"success": True, "token_data": token_data}
            finally:
                db.close()

        except httpx.HTTPError as e:
            return {"success": False, "error": f"OAuth error: {e}"}

    # -- Token refresh --

    def refresh_token(self, db: SessionLocal, account, credentials: dict = None,
                      user_id=None) -> Dict[str, Any]:
        """Refresh an expired access token using the stored refresh token."""
        import os

        api_key = _cred(credentials, "api_key") or config.ETSY_API_KEY or os.environ.get("ETSY_API_KEY") or os.environ.get("ESY_API_KEY") or ""
        api_secret = _cred(credentials, "api_secret") or config.ETSY_API_SECRET or os.environ.get("ETSY_API_SECRET") or os.environ.get("ESY_API_SECRET") or ""

        try:
            resp = httpx.post(
                ETSY_OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": api_key,
                    "refresh_token": account.refresh_token,
                },
                # Etsy documents the credential pair on the token endpoint, not
                # HTTP Basic, and a refresh needs the same keystring the
                # original authorisation used.
                headers={"x-api-key": f"{api_key}:{api_secret}"} if api_key and api_secret else {},
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

    # -- Listing fetching --

    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None,
                      user_id=None, credentials: dict = None) -> List[Dict[str, Any]]:
        """Fetch active Etsy listings via the Open API v3.

        Uses:
        - GET /v3/application/listings/active — all active listings
        - GET /v3/application/shops/{shop_id}/listings — shop-specific listings

        Paginates through results up to max_results.
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            token = self.get_token(db, user_id=user_id, credentials=credentials)
            if not token:
                return []

            api_key = _cred(credentials, "api_key") or config.ETSY_API_KEY or os.environ.get("ETSY_API_KEY") or os.environ.get("ESY_API_KEY") or ""
            headers = {
                "Authorization": f"Bearer {token['access_token']}",
                # Etsy requires the credential pair here: "<keystring>:<shared_secret>".
                # Sending the keystring alone is rejected as an invalid API key.
                "x-api-key": f"{api_key}:{api_secret}",
            }

            shop_id = token.get("shop_id") or (token.get("token_data", {}) or {}).get("shop_id")

            if not shop_id:
                # Try to discover shop_id from the API
                try:
                    resp = httpx.get(
                        f"{ETSY_API_BASE}/applications",
                        headers=headers,
                        timeout=15,
                    )
                    if resp.status_code == 200:
                        apps = resp.json()
                        for app in apps.get("results", []):
                            if app.get("type") == "SHOP_APP":
                                shop_id = app.get("shop_id")
                                break
                        if shop_id:
                            # Update stored tokens with shop_id
                            self.store_tokens(
                                db,
                                access_token=token["access_token"],
                                shop_id=shop_id,
            user_id=user_id,
                            )
                except Exception:
                    pass

            if not shop_id:
                return []

            listings = []

            # Method 1: Get active listings directly
            listings = self._fetch_active_listings(headers, shop_id, max_results)

            if not listings:
                # Method 2: Get listings by shop
                listings = self._fetch_shop_listings(headers, shop_id, max_results)

            return listings
        finally:
            if close_db:
                db.close()

    def _fetch_active_listings(self, headers: dict, shop_id: str, limit: int) -> List[dict]:
        """Fetch active listings using the /listings/active endpoint."""
        listings = []
        page = 1
        limit_per_page = 25  # Etsy max per page

        while len(listings) < limit:
            try:
                resp = httpx.get(
                    f"{ETSY_API_BASE}/application/listings/active",
                    headers=headers,
                    params={
                        "page": page,
                        "limit": min(limit_per_page, limit - len(listings)),
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("results", [])
                    if not results:
                        break
                    listings.extend(results)
                    if len(results) < limit_per_page:
                        break  # Last page
                    page += 1
                else:
                    # If /listings/active returns 404, try shop-specific endpoint
                    break
            except httpx.HTTPError:
                break

        return listings[:limit]

    def _fetch_shop_listings(self, headers: dict, shop_id: str, limit: int) -> List[dict]:
        """Fetch listings for a specific shop.

        Endpoint: GET /v3/application/shops/{shop_id}/listings
        """
        listings = []
        page = 1
        limit_per_page = 25

        while len(listings) < limit:
            try:
                resp = httpx.get(
                    f"{ETSY_API_BASE}/application/shops/{shop_id}/listings",
                    headers=headers,
                    params={
                        "page": page,
                        "limit": min(limit_per_page, limit - len(listings)),
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("results", [])
                    if not results:
                        break
                    listings.extend(results)
                    if len(results) < limit_per_page:
                        break  # Last page
                    page += 1
                else:
                    break
            except httpx.HTTPError:
                break

        return listings[:limit]

    def _normalise_listing(self, item: dict) -> Optional[dict]:
        """Normalise an Etsy API response into our standard Listing schema."""
        try:
            # Extract price
            price_cents = 0
            price_raw = ""
            price_data = item.get("price")
            if price_data:
                # Etsy stores price as a number or dict
                if isinstance(price_data, (int, float)):
                    price_cents = int(float(price_data) * 100)
                    price_raw = f"${price_cents / 100:.2f}"
                elif isinstance(price_data, dict):
                    value = price_data.get("value")
                    if isinstance(value, (int, float)):
                        price_cents = int(float(value) * 100)
                        price_raw = f"${price_cents / 100:.2f}"
                    else:
                        price_raw = str(price_data)

            # Extract images
            images = []
            for img in (item.get("images") or [])[:5]:  # Limit to 5 images
                images.append(img.get("url", ""))

            # Extract category path
            category = ""
            classif = item.get("classification", {})
            for cat_path in classif.get("categoryPath", []):
                if category:
                    category += " > "
                category += cat_path

            # Extract tags
            tags = item.get("tags", [])

            # Extract materials
            materials = item.get("materials", [])

            return {
                "platform_listing_id": str(item.get("listing_id", "")),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "price_raw": price_raw,
                "price_cents": price_cents,
                "currency": "USD",  # Etsy defaults to USD
                "status": "active",
                "is_sold": item.get("is_sold", False),
                "image_url": images[0] if images else "",
                "images_json": images,
                "category": category,
                "tags": tags,
                "materials": materials,
                "available_quantity": item.get("quantity", 0),
                "views_count": item.get("num_pending_starts", 0),
                "original_url": f"https://www.etsy.com/listing/{item.get('listing_id', '')}",
                "platform": self.PLATFORM,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            return None
