"""Amazon Seller Central (SP-API) marketplace adapter.

Implements the MarketplaceAdapter ABC for the Amazon Selling Partner API (SP-API).
Handles OAuth authentication, token refresh, and inventory listing synchronization.
"""

from typing import Any, Dict, List, Optional

import httpx

from src.adapters.base import MarketplaceAdapter, _cred
from src.database import SessionLocal
from src.config import config


class AmazonAdapter(MarketplaceAdapter):
    """Adapter for Amazon Seller Central SP-API."""

    PLATFORM = "amazon"
    PLATFORM_LABEL = "Amazon"
    MAX_REQUESTS = 30
    RATE_WINDOW_SECONDS = 60

    # Amazon SP-API endpoints
    SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"
    LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

    def get_platform_name(self) -> str:
        return "Amazon"

    def get_platform_icon(self) -> str:
        return (
            '<svg viewBox="0 0 60 30" width="50" height="25">'
            '<rect width="60" height="30" rx="4" fill="#232F3E"/>'
            '<text x="6" y="18" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#FFFFFF">amazon</text>'
            '<path d="M 10 23 Q 25 28 40 23" fill="none" stroke="#FF9900" stroke-width="2"/>'
            '</svg>'
        )

    def get_authorization_url(self, state: str = "", credentials: dict = None) -> str:
        """Build the Amazon Seller Central consent URL.

        ``redirect_uri`` is required: without it the consent page has nowhere to
        send the ``spapi_oauth_code`` after the seller approves. The earlier
        version omitted it whenever a client id was present, which made Connect
        dead on arrival.
        """
        from urllib.parse import urlencode

        client_id = _cred(credentials, "client_id") or config.AMAZON_CLIENT_ID or ""
        redirect_uri = f"{config.APP_BASE_URL}/api/auth/amazon/callback"

        params = {
            "application_id": client_id,
            "state": state,
            "version": "beta",
            "redirect_uri": redirect_uri,
        }
        return (
            "https://sellercentral.amazon.com/apps/authorize/consent?"
            + urlencode({k: v for k, v in params.items() if v})
        )

    def _amazon_credentials(self, credentials: dict) -> dict:
        """client_id/client_secret for signing the LWA token requests."""
        return {
            "client_id": _cred(credentials, "client_id") or config.AMAZON_CLIENT_ID or "",
            "client_secret": _cred(credentials, "client_secret") or config.AMAZON_CLIENT_SECRET or "",
        }

    def handle_callback(self, code: str, state: str = "", credentials: dict = None,
                        user_id=None, code_verifier: str = None,
                        seller_id: str = None) -> Dict[str, Any]:
        """Exchange the SP-API authorization code for tokens.

        Amazon's website authorization flow returns ``spapi_oauth_code`` (routed
        into ``code`` by the callback handler) and ``selling_partner_id``
        (routed into ``seller_id``). The code is exchanged at the LWA token
        endpoint for a short-lived access token and a long-lived refresh token,
        which is what makes later syncs possible.
        """
        creds = self._amazon_credentials(credentials)
        if not creds["client_id"] or not creds["client_secret"]:
            return {"success": False,
                    "error": "Amazon LWA client_id and client_secret are required."}

        redirect_uri = f"{config.APP_BASE_URL}/api/auth/amazon/callback"
        shop_name = seller_id or _cred(credentials, "seller_id") or config.AMAZON_SELLER_ID or "Amazon Seller"

        try:
            import base64
            auth = base64.b64encode(
                f"{creds['client_id']}:{creds['client_secret']}".encode()
            ).decode()
            resp = httpx.post(
                self.LWA_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                },
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                return {"success": False,
                        "error": f"Amazon token exchange failed (HTTP {resp.status_code})."}
            token_data = resp.json()

            db = SessionLocal()
            try:
                self.store_tokens(
                    db,
                    access_token=token_data["access_token"],
                    refresh_token=token_data.get("refresh_token", ""),
                    token_expires_in=token_data.get("expires_in", 3600),
                    extra_data=token_data,
                    shop_name=shop_name,
                    user_id=user_id,
                )
                return {"success": True, "shop_name": shop_name, "token_data": token_data}
            finally:
                db.close()
        except httpx.HTTPError as exc:
            return {"success": False, "error": f"Amazon OAuth error: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"Amazon OAuth error: {exc}"}

    def refresh_token(self, db: SessionLocal, account, credentials: dict = None,
                      user_id=None) -> Dict[str, Any]:
        """Exchange the stored refresh token for a new access token."""
        creds = self._amazon_credentials(credentials)
        if not account.refresh_token:
            return {"success": False, "error": "No refresh token stored."}

        try:
            resp = httpx.post(
                self.LWA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": account.refresh_token,
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                },
                timeout=30,
            )
            if resp.status_code != 200:
                return {"success": False,
                        "error": f"Amazon token refresh failed (HTTP {resp.status_code})."}
            token_data = resp.json()

            self.store_tokens(
                db,
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token", account.refresh_token),
                token_expires_in=token_data.get("expires_in", 3600),
                extra_data=token_data,
                shop_id=account.shop_id,
                shop_name=account.shop_name,
                user_id=user_id,
            )
            return {"success": True, "token_data": token_data}
        except httpx.HTTPError as exc:
            return {"success": False, "error": f"Amazon token refresh failed: {exc}"}

    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None,
                      user_id=None, credentials: dict = None) -> List[Dict[str, Any]]:
        """Fetch active listings from Amazon Seller Central (SP-API)."""
        should_close = False
        if db is None:
            db = SessionLocal()
            should_close = True

        try:
            results = []

            # get_token also refreshes an expired access token using the stored
            # refresh token and this user's own LWA credentials.
            token = self.get_token(db, user_id=user_id, credentials=credentials)
            if not token or not token.get("access_token"):
                self.last_error = (
                    "Amazon is not connected. Connect the account first; the "
                    "refresh token is obtained during authorisation."
                )
                return []

            try:
                with httpx.Client(timeout=10) as client:
                    headers = {
                        "x-amz-access-token": token["access_token"],
                        "User-Agent": "MarketplaceDashboard/1.0",
                    }
                    marketplace_id = config.AMAZON_MARKETPLACE_ID or ""
                    if not marketplace_id:
                        self.last_error = (
                            "AMAZON_MARKETPLACE_ID is not configured, so there "
                            "is no marketplace to read inventory from."
                        )
                        return []

                    # 2. Fetch inventory summary
                    inv_url = (
                        f"{self.SP_API_BASE}/fba/inventory/v1/summaries?"
                        f"details=true&granularityType=Marketplace&"
                        f"granularityId={marketplace_id}&marketplaceIds={marketplace_id}"
                    )
                    inv_resp = client.get(inv_url, headers=headers)
                    if inv_resp.status_code != 200:
                        self.last_error = (
                            f"Amazon returned HTTP {inv_resp.status_code} "
                            f"fetching inventory."
                        )
                        return []

                    inv_data = inv_resp.json()
                    for item in inv_data.get("payload", {}).get("inventorySummaries", [])[:max_results]:
                        asin = item.get("asin")
                        sku = item.get("sellerSku", "")
                        qty = item.get("totalQuantity", 0)
                        results.append({
                            "platform": self.PLATFORM,
                            "platform_listing_id": asin or sku,
                            "title": item.get("productName") or f"Amazon Item ({asin or sku})",
                            "description": f"ASIN: {asin or ''} | SKU: {sku or ''} | FBA Inventory",
                            # The inventory summary does not return price or
                            # images; do not invent them.
                            "price_cents": 0,
                            "price_raw": "",
                            "currency": "USD",
                            "status": "active" if qty > 0 else "sold",
                            "is_sold": qty <= 0,
                            "available_quantity": qty,
                            "views_count": 0,
                            "image_url": "",
                            "images": [],
                            "original_url": f"https://www.amazon.com/dp/{asin}" if asin else "",
                            "sku": sku,
                            "category": "Amazon FBA",
                        })
            except Exception as exc:
                self.last_error = f"Amazon sync failed: {exc}"

            return results
        finally:
            if should_close:
                db.close()
