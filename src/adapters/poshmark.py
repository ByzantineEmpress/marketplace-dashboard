"""Poshmark marketplace adapter.

Implements the MarketplaceAdapter ABC for Poshmark closet integration.
Supports closet inventory syncing, status tracking, and credential management.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from src.adapters.base import MarketplaceAdapter, _cred
from src.database import SessionLocal
from src.config import config
from src.models import MarketplaceAccount


class PoshmarkAdapter(MarketplaceAdapter):
    """Adapter for Poshmark closets and inventory."""

    PLATFORM = "poshmark"
    PLATFORM_LABEL = "Poshmark"
    MAX_REQUESTS = 30
    RATE_WINDOW_SECONDS = 60

    def get_platform_name(self) -> str:
        return "Poshmark"

    def get_platform_icon(self) -> str:
        return (
            '<svg viewBox="0 0 60 30" width="50" height="25">'
            '<rect width="60" height="30" rx="4" fill="#8E1A34"/>'
            '<text x="6" y="20" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#FFFFFF">POSH</text>'
            '</svg>'
        )

    def get_authorization_url(self, state: str = "", credentials: dict = None) -> str:
        """Return the Poshmark connection URL."""
        username = _cred(credentials, "username") or config.POSHMARK_USERNAME or "closet"
        return f"https://poshmark.com/closet/{username}"

    def handle_callback(self, code: str, state: str = "", credentials: dict = None,
                        user_id=None) -> Dict[str, Any]:
        """Handle Poshmark connection confirmation."""
        username = _cred(credentials, "username") or config.POSHMARK_USERNAME or "my_posh_closet"
        db = SessionLocal()
        try:
            account = self._find_account(db, user_id)
            if not account:
                account = MarketplaceAccount(
                    platform=self.PLATFORM,
                    shop_name=username,
                    is_connected=True,
                    last_synced=datetime.utcnow(),
                )
                db.add(account)
            else:
                account.shop_name = username
                account.is_connected = True
                account.last_synced = datetime.utcnow()
            db.commit()
            return {"success": True, "shop_name": username}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}
        finally:
            db.close()

    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None,
                      user_id=None, credentials: dict = None) -> List[Dict[str, Any]]:
        """Fetch active listings from the connected Poshmark closet."""
        should_close = False
        if db is None:
            db = SessionLocal()
            should_close = True

        try:
            account = self._find_account(db, user_id)
            username = _cred(credentials, "username") or config.POSHMARK_USERNAME or (account.shop_name if account else "")

            if not username:
                self.last_error = (
                    "No Poshmark closet is configured. Save your closet "
                    "username, then try again."
                )
                return []

            results = []
            try:
                url = f"https://poshmark.com/vm-rest/users/{username}/posts"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                }
                with httpx.Client(timeout=10) as client:
                    resp = client.get(url, headers=headers)
                    if resp.status_code != 200:
                        self.last_error = (
                            f"Poshmark returned HTTP {resp.status_code} for "
                            f"closet '{username}'."
                        )
                        return []

                    data = resp.json()
                    for item in data.get("data", [])[:max_results]:
                        price_val = float(item.get("price", 0) or 0)
                        price_cents = int(price_val * 100)
                        is_sold = item.get("inventory", {}).get("status") == "sold_out"
                        results.append({
                            "platform": self.PLATFORM,
                            "platform_listing_id": str(item.get("id")),
                            "title": item.get("title") or "Poshmark Item",
                            "description": item.get("description", ""),
                            "price_cents": price_cents,
                            "price_raw": f"USD {price_val:.2f}",
                            "currency": "USD",
                            "status": "sold" if is_sold else "active",
                            "is_sold": is_sold,
                            "available_quantity": 0 if is_sold else 1,
                            "views_count": item.get("views_count", 0),
                            "image_url": (item.get("pictures") or [{}])[0].get("url") or "",
                            "images": [p.get("url") for p in item.get("pictures", []) if p.get("url")],
                            "original_url": f"https://poshmark.com/listing/{item.get('id')}",
                            "category": item.get("category", {}).get("name") if isinstance(item.get("category"), dict) else str(item.get("category") or ""),
                            "sku": item.get("sku") or "",
                        })
            except Exception as exc:
                # Surface the reason rather than substituting demo inventory.
                self.last_error = f"Poshmark sync failed: {exc}"

            return results
        finally:
            if should_close:
                db.close()
