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

        # eBay scope URNs. The form is
        # "https://api.ebay.com/oauth/api_scope/<name>".
        #
        # Each one earns its place:
        #   api_scope                  — the base scope every Sell call needs
        #   sell.inventory.readonly    — Inventory API (inventory-managed items)
        #   sell.listing.read          — "View eBay listings". This is the scope
        #                                behind the Marketplace Listing API, which
        #                                is the only REST endpoint that returns a
        #                                seller's ACTIVE listings regardless of how
        #                                they were created. Without it that endpoint
        #                                answers 404 rather than 403, which is why
        #                                a sync returned nothing while the account
        #                                plainly had listings.
        #   commerce.identity.readonly — "View a user's basic information, such as
        #                                username". Used to record WHICH eBay user a
        #                                connection belongs to, so an eBay
        #                                account-deletion notification can be matched
        #                                to our stored data.
        #
        # Deliberately NOT requested: anything under /buy/. The Buy API (Browse)
        # would carry titles and photos too, but eBay has not granted this app any
        # buy.* scope, so it is not an option here.
        scopes = [
            "https://api.ebay.com/oauth/api_scope",
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.listing.read",
            "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
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

    def _fetch_identity(self, access_token: str) -> dict:
        """The eBay user behind a token: username, immutable userId, and the
        marketplace they are registered on.

        ``commerce.identity.readonly`` scope. Base is apiz.ebay.com (not
        api.ebay.com) — eBay splits its APIs across two hosts. Returns {} on any
        failure so a connect still succeeds without it.
        """
        try:
            resp = httpx.get(
                "https://apiz.ebay.com/commerce/identity/v1/user/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

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

            # Store the tokens, then record the eBay user's identity so an eBay
            # account-deletion notification can be matched back to this row.
            db = SessionLocal()
            try:
                account = self.store_tokens(
                    db,
                    access_token=token_data["access_token"],
                    refresh_token=token_data.get("refresh_token", ""),
                    token_expires_in=token_data.get("expires_in", 7200),
                    extra_data=token_data,
                    user_id=user_id,
                )

                identity = self._fetch_identity(token_data["access_token"])
                if identity:
                    account.shop_name = identity.get("username") or account.shop_name
                    account.shop_id = identity.get("userId") or account.shop_id
                    merged = dict(account.token_data or {})
                    merged["registration_marketplace_id"] = identity.get(
                        "registrationMarketplaceId") or ""
                    account.token_data = merged
                    db.commit()

                return {"success": True, "token_data": token_data, "identity": identity}
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
        """Fetch the seller's active eBay listings.

        eBay has no simple REST endpoint that returns every active listing for a
        seller, so this uses the Sell Feed API's Active Inventory Report (async:
        create a task, poll, download a zipped XML). It returns ItemID, price,
        currency and quantity for listings created any way — including Seller Hub
        listings, which the Inventory API does not see.

        Titles and photos are NOT in that report, so each row gets a generated
        title and a deep link; they can be renamed/annotated in the dashboard.
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

            # The report is the one source that sees every listing. The inventory
            # path is retained as a fallback for accounts managed via the
            # Inventory API, but it is empty for typical Seller Hub sellers.
            marketplace = (token.get("registration_marketplace_id") or "").strip() or "EBAY_US"
            listings = self._fetch_active_inventory_report(headers, max_results, marketplace)
            if listings:
                return self._enrich_listings(listings, headers, marketplace, max_results)

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

    def _fetch_active_inventory_report(self, headers: dict, limit: int,
                                       marketplace: str) -> List[dict]:
        """Download eBay's Active Inventory Report and normalise it.

        Flow: create one inventory task (feedType=LMS_ACTIVE_INVENTORY_REPORT),
        poll until COMPLETED, download the result (a ZIP containing XML), and
        parse each <SKUDetails> into a listing-shaped dict.

        One task for the seller's registration marketplace is enough: the report
        covers the seller's listings regardless of how they were created, unlike
        the Inventory API.
        """
        import io
        import re
        import time
        import xml.etree.ElementTree as ET
        import zipfile

        base = f"{EBAY_API_BASE}/sell/feed/v1"

        try:
            create = httpx.post(
                f"{base}/inventory_task",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "feedType": "LMS_ACTIVE_INVENTORY_REPORT",
                    "marketplaceId": marketplace,
                },
                timeout=30,
            )
            if create.status_code not in (200, 201, 202):
                self.last_error = f"eBay report task HTTP {create.status_code}"
                return []

            location = create.headers.get("location", "")
            match = re.search(r"task/([^?]+)", location)
            if not match:
                self.last_error = "eBay report task returned no task id"
                return []
            task_id = match.group(1)

            status = ""
            for _ in range(15):
                time.sleep(2)
                check = httpx.get(f"{base}/inventory_task/{task_id}",
                                  headers=headers, timeout=30)
                status = (check.json() or {}).get("status", "") if check.status_code == 200 else ""
                if status == "COMPLETED":
                    break
            if status != "COMPLETED":
                self.last_error = (
                    f"eBay report task did not complete (status={status or 'unknown'})")
                return []

            dl = httpx.get(f"{base}/task/{task_id}/download_result_file",
                           headers=headers, timeout=60)
            if dl.status_code != 200:
                self.last_error = f"eBay report download HTTP {dl.status_code}"
                return []

            rows = self._parse_active_inventory_report(dl.content)
            return rows[:limit]
        except Exception as exc:
            self.last_error = f"eBay inventory report failed: {exc}"
            return []

    def _parse_active_inventory_report(self, data: bytes) -> List[dict]:
        """Parse a zipped ActiveInventoryReport XML into listing-shaped dicts.

        The report gives only ItemID, price, currency and quantity — no title or
        image — so the title is generated and the image left empty.
        """
        import io
        import xml.etree.ElementTree as ET
        import zipfile

        items = []
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            xml_bytes = z.read(z.namelist()[0])
            root = ET.fromstring(xml_bytes)
        except Exception:
            return []

        for sku in root.iter():
            if sku.tag.rsplit("}", 1)[-1] != "SKUDetails":
                continue

            item_id, price_cents, currency, qty = "", 0, "CAD", 0
            for child in sku:
                tag = child.tag.rsplit("}", 1)[-1]
                text = (child.text or "").strip()
                if tag == "ItemID":
                    item_id = text
                elif tag == "Price":
                    currency = child.attrib.get("currencyID", "CAD")
                    try:
                        price_cents = int(round(float(text) * 100))
                    except ValueError:
                        price_cents = 0
                elif tag == "Quantity":
                    try:
                        qty = int(text)
                    except ValueError:
                        qty = 0

            if not item_id:
                continue

            items.append({
                "platform_listing_id": item_id,
                # The report has no title, so it is generated. The dashboard can
                # rename it; the deep link below is how to find the real listing.
                "title": f"eBay listing {item_id}",
                "description": "",
                "price_cents": price_cents,
                "price_raw": f"{currency} {price_cents / 100:.2f}",
                "currency": currency,
                "status": "active",
                "is_sold": False,
                "image_url": "",
                "images_json": [],
                "available_quantity": qty,
                "views_count": 0,
                "original_url": f"https://www.ebay.com/itm/{item_id}",
                "sku": "",
                "category": "",
                "platform": self.PLATFORM,
            })

        return items

    def _fetch_browse_item(self, item_id: str, headers: dict) -> dict:
        """Fetch one item's title, image(s), category and description.

        The Active Inventory Report has only ItemID/price/quantity, so this uses
        the Browse API's item endpoint with the ``v1|<legacy item id>|0`` id form
        (the legacy ItemID alone 404s). It accepts the same user token — no buy.*
        scope is required for the item resource, unlike item_summary/search.
        """
        try:
            resp = httpx.get(
                f"https://api.ebay.com/buy/browse/v1/item/v1|{item_id}|0",
                headers=headers,
                timeout=30,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            image = data.get("image") or {}
            images = [image.get("imageUrl")] if image.get("imageUrl") else []
            for extra in data.get("additionalImages") or []:
                url = (extra or {}).get("imageUrl") if isinstance(extra, dict) else None
                if url:
                    images.append(url)
            return {
                "title": data.get("title", ""),
                "image_url": image.get("imageUrl", ""),
                "images": images,
                "category": data.get("categoryPath", ""),
                "description": data.get("description") or "",
            }
        except Exception:
            return {}

    def _enrich_listings(self, rows: List[dict], headers: dict, marketplace: str,
                         limit: int) -> List[dict]:
        """Fill in title, image and category for each report row.

        The report alone yields generated titles and no photos; the Browse API
        supplies the real values per item. A row whose item cannot be fetched is
        left as-is, so a transient Browse failure never drops a listing.
        """
        import time

        browse_headers = dict(headers)
        browse_headers["X-EBAY-C-MARKETPLACE-ID"] = marketplace

        enriched = []
        for row in rows[:limit]:
            detail = self._fetch_browse_item(row["platform_listing_id"], browse_headers)
            if detail.get("title"):
                row["title"] = detail["title"]
            if detail.get("image_url"):
                row["image_url"] = detail["image_url"]
            if detail.get("images"):
                row["images_json"] = detail["images"]
            if detail.get("category"):
                row["category"] = detail["category"]
            if detail.get("description"):
                row["description"] = detail["description"]
            enriched.append(row)
            # A short pause between calls keeps the burst well under the Browse
            # API's per-call rate limit even for a large inventory.
            time.sleep(0.1)

        return enriched

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
