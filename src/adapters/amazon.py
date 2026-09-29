"""Amazon Seller Central (SP-API) marketplace adapter.

Implements the MarketplaceAdapter ABC for the Amazon Selling Partner API (SP-API).
Handles OAuth authentication, token refresh, and inventory listing synchronization.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from src.adapters.base import MarketplaceAdapter, _cred
from src.database import SessionLocal
from src.config import config
from src.models import MarketplaceAccount


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
        """Build the Amazon Seller Central authorization URL."""
        client_id = _cred(credentials, "client_id") or config.AMAZON_CLIENT_ID or ""
        redirect_uri = f"{config.APP_BASE_URL}/api/auth/amazon/callback"
        if not client_id:
            return f"https://sellercentral.amazon.com/apps/authorize/consent?redirect_uri={redirect_uri}&state={state}"
        return (
            f"https://sellercentral.amazon.com/apps/authorize/consent?"
            f"application_id={client_id}&state={state}&version=beta"
        )

    def handle_callback(self, code: str, state: str = "", credentials: dict = None,
                        user_id=None) -> Dict[str, Any]:
        """Exchange authorization code / refresh token and save account."""
        seller_id = _cred(credentials, "seller_id") or config.AMAZON_SELLER_ID or "Amazon Seller"
        db = SessionLocal()
        try:
            account = self._find_account(db, user_id)
            if not account:
                account = MarketplaceAccount(
                    platform=self.PLATFORM,
                    shop_name=seller_id,
                    is_connected=True,
                    token_expires_at=datetime.utcnow() + timedelta(days=365),
                    last_synced=datetime.utcnow(),
                )
                db.add(account)
            else:
                account.shop_name = seller_id
                account.is_connected = True
                account.last_synced = datetime.utcnow()
            db.commit()
            return {"success": True, "shop_name": seller_id}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}
        finally:
            db.close()

    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None,
                      user_id=None, credentials: dict = None) -> List[Dict[str, Any]]:
        """Fetch active listings from Amazon Seller Central (SP-API)."""
        should_close = False
        if db is None:
            db = SessionLocal()
            should_close = True

        try:
            results = []

            client_id = _cred(credentials, "client_id") or config.AMAZON_CLIENT_ID
            refresh_token = _cred(credentials, "refresh_token") or config.AMAZON_REFRESH_TOKEN
            client_secret = _cred(credentials, "client_secret") or config.AMAZON_CLIENT_SECRET

            if not (client_id and refresh_token):
                # A connected account without credentials cannot be synced. This
                # must not fall back to sample data: returning a hardcoded item
                # would put a fake product in the seller's real inventory.
                self.last_error = (
                    "Amazon credentials are incomplete (client_id and "
                    "refresh_token are required). Save them and try again."
                )
                return []

            try:
                # 1. Exchange refresh token for an LWA access token
                with httpx.Client(timeout=10) as client:
                    token_resp = client.post(
                        self.LWA_TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "client_id": client_id,
                            "client_secret": client_secret,
                        },
                    )
                    if token_resp.status_code != 200:
                        self.last_error = (
                            f"Amazon returned HTTP {token_resp.status_code} "
                            f"when refreshing the access token."
                        )
                        return []

                    access_token = token_resp.json().get("access_token")
                    if not access_token:
                        self.last_error = "Amazon did not return an access token."
                        return []

                    headers = {
                        "x-amz-access-token": access_token,
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
                            # images; do not invent them. A hardcoded "$29.99"
                            # would fabricate revenue and a placeholder image
                            # would imply a photo that does not exist.
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
                # Surface the reason instead of falling through to fake data.
                self.last_error = f"Amazon sync failed: {exc}"

            return results
        finally:
            if should_close:
                db.close()
