"""Etsy Open API v3 adapter.

Implements the MarketplaceAdapter ABC for Etsy's REST API.

Auth: OAuth 2.0 with Etsy's Seller App access.
Endpoints used:
- GET /v3/application/shops/{shop_id}/listings — this shop's active listings
- GET /v3/application/users/{user_id}/shops — resolve the shop at connect time

Note: /v3/application/listings/active is NOT used. It is Etsy's public
marketplace-wide feed and returns listings from every seller on Etsy, which is
not something this application should ever read.

References:
- https://developers.etsy.com/documentation/apis/reference/shop-listing
"""

import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from src.adapters.base import MarketplaceAdapter, _cred, _oauth_error
from src.database import SessionLocal
from src.models import Listing
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

        # Required scopes.
        #
        # shops_r is needed to resolve the seller's shop: Etsy's token response
        # has no shop_id, and /v3/application/users/{id}/shops returns 403
        # without it. Requesting only listings_r meant the shop could never be
        # found, so every sync silently reported 0 listings.
        #
        # transactions_r is what the Receipts API needs — the only way to see an
        # order, and so the only way to learn that something SOLD and for how
        # much. Without it a sale never reaches the dashboard and has to be
        # marked by hand.
        scopes = "listings_r shops_r transactions_r"

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
                        user_id=None, code_verifier: str = "",
                        seller_id: str = None) -> Dict[str, Any]:
        """Exchange an authorisation code for access and refresh tokens.

        Also stores the shop_id from the response.

        ``code_verifier`` is the PKCE verifier whose S256 challenge was sent with
        the authorisation request. Etsy requires it on the token request; without
        it the exchange fails with a bare "code_verifier is required".

        ``seller_id`` is accepted and ignored: the shared callback route passes it
        for Amazon, and this signature must match that call. Omitting it here is
        what made the route fall back to a call without the verifier.
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
            if resp.status_code >= 400:
                # Etsy explains the rejection in the body ("invalid_grant",
                # "code_verifier is invalid", a scope problem). Surfacing only
                # "400 Bad Request" made this impossible to diagnose from the UI.
                return {"success": False, "error": _oauth_error(resp, "Etsy")}
            resp.raise_for_status()
            token_data = resp.json()

            # Etsy's token response carries user_id, never shop_id, so the shop
            # has to be resolved with a second call. Done here as well as in
            # list_listings so the shop is known immediately after connecting.
            db = SessionLocal()
            try:
                api_secret_local = _cred(credentials, "api_secret") or config.ETSY_API_SECRET or os.environ.get("ETSY_API_SECRET") or os.environ.get("ESY_API_SECRET") or ""
                shop_headers = {
                    "Authorization": f"Bearer {token_data.get('access_token', '')}",
                    "x-api-key": f"{api_key}:{api_secret_local}",
                }
                resolved = self.resolve_shop(
                    shop_headers, token_data, token_data.get("access_token", "")
                )

                self.store_tokens(
                    db,
                    access_token=token_data["access_token"],
                    refresh_token=token_data.get("refresh_token", ""),
                    token_expires_in=token_data.get("expires_in", 7200),
                    extra_data=token_data,
                    shop_id=resolved.get("shop_id"),
                    shop_name=resolved.get("shop_name") or None,
            user_id=user_id,
                )
                db.commit()

                return {"success": True, "token_data": token_data, "shop": resolved}
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
            if resp.status_code >= 400:
                return {"success": False, "error": _oauth_error(resp, "Etsy")}
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

    def resolve_shop(self, headers: dict, token_data: dict, access_token: str = "") -> dict:
        """Find the signed-in seller's shop.

        Etsy's token response contains ``user_id`` and NOT ``shop_id``, so the
        shop has to be looked up separately. The endpoint is
        ``GET /v3/application/users/{user_id}/shops``, which requires the
        ``shops_r`` scope.

        Returns ``{"shop_id": ..., "shop_name": ...}`` with whatever could be
        determined, or ``{}`` on failure. Never raises: a missing shop must not
        break the OAuth callback, it should surface as a clear message later.

        This replaced a lookup against ``GET /v3/application/applications``,
        which returns the *applications* a user has authorised and only carries
        a shop for entries of type SHOP_APP. For a normal seller it returns
        nothing, which is why every sync reported "0 listings" with no error.
        """
        user_id = str(token_data.get("user_id") or "")

        # Etsy access tokens are "<user_id>.<opaque>", so this is a reliable
        # fallback when the token response predates us storing user_id.
        if not user_id and access_token and "." in access_token:
            candidate = access_token.split(".", 1)[0]
            if candidate.isdigit():
                user_id = candidate

        if not user_id:
            return {}

        try:
            resp = httpx.get(
                f"{ETSY_API_BASE}/application/users/{user_id}/shops",
                headers=headers,
                timeout=15,
            )
        except Exception:
            return {}

        if resp.status_code != 200:
            # 403 here means the token lacks shops_r, so the seller needs to
            # reconnect. Recorded rather than swallowed, because "0 listings"
            # with no explanation is what made this hard to diagnose.
            try:
                body = resp.json()
            except Exception:
                body = {}
            return {"error": body.get("error") or f"HTTP {resp.status_code}"}

        try:
            payload = resp.json() or {}
        except Exception:
            return {}

        # The endpoint returns the shop object directly; tolerate a wrapped form
        # in case Etsy changes it.
        shop = payload
        if "results" in payload and isinstance(payload["results"], list) and payload["results"]:
            shop = payload["results"][0]

        return {
            "shop_id": shop.get("shop_id"),
            "shop_name": shop.get("shop_name") or "",
        }

    def list_listings(self, max_results: int = 500, db: Optional[SessionLocal] = None,
                      user_id=None, credentials: dict = None) -> List[Dict[str, Any]]:
        """Fetch this shop's active Etsy listings.

        Uses only GET /v3/application/shops/{shop_id}/listings.

        The public /v3/application/listings/active feed is deliberately NOT
        used: it returns listings from every seller on Etsy, and using it once
        imported 900 pages of other people's inventory.

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
            # The shared secret is half of the x-api-key pair Etsy requires
            # on every request, so it has to be resolved here too.
            api_secret = _cred(credentials, "api_secret") or config.ETSY_API_SECRET or os.environ.get("ETSY_API_SECRET") or os.environ.get("ESY_API_SECRET") or ""
            headers = {
                "Authorization": f"Bearer {token['access_token']}",
                # Etsy requires the credential pair here: "<keystring>:<shared_secret>".
                # Sending the keystring alone is rejected as an invalid API key.
                "x-api-key": f"{api_key}:{api_secret}",
            }

            shop_id = token.get("shop_id") or (token.get("token_data", {}) or {}).get("shop_id")

            if not shop_id:
                # Resolve it from the API. This is the normal path: Etsy's token
                # response has no shop_id.
                resolved = self.resolve_shop(
                    headers, token.get("token_data", {}) or {}, token.get("access_token", "")
                )
                shop_id = resolved.get("shop_id")
                if shop_id:
                    # Persist so later syncs skip this call.
                    try:
                        self.store_tokens(
                            db,
                            access_token=token["access_token"],
                            shop_id=shop_id,
                            shop_name=resolved.get("shop_name") or None,
                            user_id=user_id,
                        )
                        db.commit()
                    except Exception:
                        db.rollback()

            if not shop_id:
                # No shop means nothing to fetch. The caller reports this rather
                # than a silent zero.
                self.last_error = (
                    "Could not determine your Etsy shop. Reconnect the account so "
                    "the shops_r permission is granted, then try again."
                )
                return []

            # One source only: this shop's own listings.
            #
            # There used to be two "methods", the first of which called Etsy's
            # public marketplace feed. That is where 900 pages of other people's
            # listings came from. There is no fallback now: if the shop endpoint
            # fails, that is reported, because reading the public feed instead
            # would be far worse than returning nothing.
            listings = self._fetch_shop_listings(headers, shop_id, max_results)
            return listings
        finally:
            if close_db:
                db.close()

    # -- Sales (receipts) --

    def sync_sales(self, db, user_id=None, credentials: dict = None,
                   days: int = 90) -> Dict[str, Any]:
        """Pull Etsy receipts (completed orders) and record them as sales.

        Receipts are Etsy's equivalent of eBay orders, and like eBay's listing
        endpoints every listing call reads ACTIVE listings only — so a sale is
        invisible until it is read from here.

        Fees come from the payment ledger: a receipt's own fee entries reference
        it, and Etsy's transaction fee references the receipt's transaction.
        """
        import os as _os
        import time as _time

        if not credentials:
            credentials = {}

        token = self.get_token(db, user_id=user_id, credentials=credentials)
        if not token:
            return {"supported": True, "success": False, "fetched": 0,
                    "recorded": 0, "created": 0,
                    "error": "No valid access token — connect the account first"}

        api_key = _cred(credentials, "api_key") or config.ETSY_API_KEY or _os.environ.get("ETSY_API_KEY") or _os.environ.get("ESY_API_KEY") or ""
        api_secret = _cred(credentials, "api_secret") or config.ETSY_API_SECRET or _os.environ.get("ETSY_API_SECRET") or _os.environ.get("ESY_API_SECRET") or ""
        headers = {
            "Authorization": f"Bearer {token['access_token']}",
            # Etsy requires the credential pair here: "<keystring>:<shared_secret>".
            "x-api-key": f"{api_key}:{api_secret}",
        }

        shop_id = token.get("shop_id") or (token.get("token_data", {}) or {}).get("shop_id")
        if not shop_id:
            resolved = self.resolve_shop(
                headers, token.get("token_data", {}) or {}, token.get("access_token", "")
            )
            shop_id = resolved.get("shop_id")
        if not shop_id:
            return {"supported": True, "success": False, "fetched": 0,
                    "recorded": 0, "created": 0,
                    "error": "Could not determine your Etsy shop. Reconnect the account."}

        cutoff = int(_time.time()) - max(1, min(int(days), 90)) * 86400

        try:
            receipts = self._fetch_receipts(headers, shop_id, cutoff)
        except Exception as exc:
            return {"supported": True, "success": False, "fetched": 0,
                    "recorded": 0, "created": 0, "error": f"Etsy receipts failed: {exc}"}

        if not receipts:
            return {"supported": True, "success": True, "fetched": 0,
                    "recorded": 0, "created": 0}

        try:
            entries = self._fetch_ledger_entries(headers, shop_id, days)
        except Exception:
            entries = []

        fees_by_receipt = self._fees_by_receipt(entries)
        shipping_by_receipt = self._label_costs_by_receipt(entries, receipts)

        sales = []
        for receipt in receipts:
            sales.extend(
                self._sales_from_receipt(receipt, fees_by_receipt, shipping_by_receipt)
            )

        # Receipts carry no photos, and a sold item is not in the active-listing
        # sync, so its picture has to be fetched here or the sold card is blank.
        try:
            self._attach_sale_images(db, sales, headers)
        except Exception as exc:
            self.last_error = f"Etsy sale images failed: {exc}"

        applied = self.record_sales(db, sales, owner_user_id=user_id)
        return {"supported": True, "success": True, "fetched": len(sales),
                "recorded": applied["recorded"], "created": applied["created"]}

    def _attach_sale_images(self, db, sales: List[dict], headers: dict) -> int:
        """Give each Etsy sale a picture.

        Etsy serves a sold listing's images long after it sells: the state is
        ``sold_out`` and ``/listings/{id}/images`` still returns them, which the
        ``include=Images`` parameter on the listing itself does not.

        Items whose picture is already stored are skipped, so a repeat sync does
        not re-fetch every sale.
        """
        import time

        ids = [s["platform_listing_id"] for s in sales if s.get("platform_listing_id")]
        if not ids:
            return 0

        already = {
            row[0]
            for row in db.query(Listing.platform_listing_id).filter(
                Listing.platform == self.PLATFORM,
                Listing.platform_listing_id.in_(ids),
                Listing.image_url.isnot(None),
                Listing.image_url != "",
            ).all()
        }

        attached = 0
        for sale in sales:
            item_id = sale.get("platform_listing_id")
            if not item_id or item_id in already or sale.get("image_url"):
                continue
            try:
                resp = httpx.get(
                    f"{ETSY_API_BASE}/application/listings/{item_id}/images",
                    headers=headers, timeout=30,
                )
                if resp.status_code != 200:
                    continue
                raw = (resp.json() or {}).get("results") or []
                raw = [i for i in raw if isinstance(i, dict)]
                raw.sort(key=lambda i: i.get("rank", 0) or 0)
                urls = []
                for img in raw[:10]:
                    url = (img.get("url_570xN") or img.get("url_fullxfull")
                           or img.get("url_170x135") or img.get("url_75x75"))
                    if url:
                        urls.append(url)
                if urls:
                    sale["image_url"] = urls[0]
                    sale["images"] = urls
                    attached += 1
            except Exception:
                continue
            time.sleep(0.1)
        return attached

    def _fetch_receipts(self, headers: dict, shop_id, cutoff: int) -> list:
        """Completed receipts newer than the cutoff.

        Etsy caps a ledger window at 31 days and pages by offset; receipts are
        paged the same way and filtered on their own timestamp, because the
        receipt endpoint's date filter is unreliable across shop types.
        """
        receipts = []
        offset = 0
        page_size = 100
        while offset < 1000:
            resp = httpx.get(
                f"{ETSY_API_BASE}/application/shops/{shop_id}/receipts",
                headers=headers,
                params={"limit": page_size, "offset": offset, "was_paid": "true"},
                timeout=60,
            )
            if resp.status_code != 200:
                if not receipts:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                break
            batch = (resp.json() or {}).get("results") or []
            for receipt in batch:
                created = receipt.get("created_timestamp") or 0
                if created and created < cutoff:
                    continue
                status = (receipt.get("status") or "").lower()
                # Canceled orders are not sales; refunded ones still are, and the
                # refund is a separate ledger movement.
                if status in ("canceled", "cancelled"):
                    continue
                receipts.append(receipt)
            if len(batch) < page_size:
                break
            offset += page_size
        return receipts

    def _fetch_ledger_entries(self, headers: dict, shop_id, days: int) -> list:
        """The shop's payment ledger, newest window first.

        Etsy refuses a window wider than 31 days, so it is walked a month at a
        time. Walking newest-first means a shop with a long history still gets its
        recent months before the page cap is reached.
        """
        import time as _time

        span = max(1, min(int(days), 900)) * 86400
        now = int(_time.time())
        entries = []
        newest = now

        while newest > now - span:
            oldest = max(newest - 30 * 86400, now - span)
            offset = 0
            while offset < 1000:
                resp = httpx.get(
                    f"{ETSY_API_BASE}/application/shops/{shop_id}/payment-account/ledger-entries",
                    headers=headers,
                    params={"min_created": oldest, "max_created": newest,
                            "limit": 100, "offset": offset},
                    timeout=60,
                )
                if resp.status_code != 200:
                    break
                batch = (resp.json() or {}).get("results") or []
                entries.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100
            newest = oldest

        return entries

    @staticmethod
    def _fees_by_receipt(entries: list) -> dict:
        """Etsy's charges per receipt.

        Etsy does not put fees on the receipt. The ledger does, and a fee entry
        points at its receipt either directly (reference_type "receipt": sales
        tax, operating fee, shipping transaction, VAT) or through the receipt's
        transaction, so both keys are used.
        """
        fees: Dict[int, int] = {}
        for entry in entries:
            amount = int(entry.get("amount") or 0)
            # Only money OUT is a cost; PAYMENT_GROSS is the buyer's money.
            if amount >= 0:
                continue
            if (entry.get("reference_type") or "").lower() != "receipt":
                continue
            ref_id = entry.get("reference_id")
            if ref_id:
                fees[int(ref_id)] = fees.get(int(ref_id), 0) + abs(amount)
        return fees

    @staticmethod
    def _label_costs_by_receipt(entries: list, receipts: list) -> dict:
        """What the seller actually paid for postage, per receipt.

        Etsy records a label as its own ledger movement referencing the LABEL, not
        the receipt, so there is no id to join on. Two things make the link
        reliable: the label's charges (the label itself, its tax and any later
        adjustment all share one label id) are totalled first, then matched to the
        receipt whose shipment notification is nearest in time.

        The match is bounded to a day either side. Without the bound, an
        adjustment made days later lands on whichever receipt happens to be
        closest and silently inflates its cost.
        """
        labels: Dict[object, dict] = {}
        for entry in entries:
            amount = int(entry.get("amount") or 0)
            if amount >= 0:
                continue
            if (entry.get("reference_type") or "").lower() != "shipping_label":
                continue
            ref_id = entry.get("reference_id")
            if ref_id is None:
                continue
            record = labels.setdefault(ref_id, {"amount": 0, "time": None})
            record["amount"] += abs(amount)
            # The label charge itself fixes the purchase time. A later adjustment
            # must not drag the total away from the receipt it belongs to.
            stamp = entry.get("create_date")
            if (entry.get("ledger_type") or "") == "shipping_labels":
                record["time"] = stamp
            elif not record["time"]:
                record["time"] = stamp

        if not labels:
            return {}

        ships = []
        for receipt in receipts:
            stamps = [
                (s or {}).get("shipment_notification_timestamp")
                for s in (receipt.get("shipments") or [])
            ]
            stamps = [s for s in stamps if s]
            # Fall back to the receipt's own time for an order not yet marked
            # shipped: the label is normally bought around then.
            when = min(stamps) if stamps else receipt.get("created_timestamp")
            if when:
                ships.append((int(when), receipt.get("receipt_id")))

        if not ships:
            return {}

        window = 24 * 3600
        costs: Dict[int, int] = {}
        for record in labels.values():
            when = record["time"]
            if not when:
                continue
            gap, receipt_id = min((abs(stamp - when), rid) for stamp, rid in ships)
            if gap <= window and receipt_id is not None:
                costs[int(receipt_id)] = costs.get(int(receipt_id), 0) + record["amount"]
        return costs

    def _sales_from_receipt(self, receipt: dict, fees_by_receipt: dict,
                            shipping_by_receipt: dict = None) -> List[dict]:
        """One entry per receipt line item, shaped for record_sales().

        ``grandtotal`` is what the buyer actually paid, which is the honest
        revenue figure: it already includes the shipping they were charged and the
        tax Etsy remits on the seller's behalf.
        """
        from datetime import datetime

        receipt_id = receipt.get("receipt_id")
        created = receipt.get("created_timestamp") or 0
        sold_at = datetime.utcfromtimestamp(created) if created else None
        currency = ((receipt.get("grandtotal") or {}).get("currency_code")) or "CAD"
        # Fees are recorded per receipt, so they are split across its items.
        fees_cents = int(fees_by_receipt.get(int(receipt_id) if receipt_id else 0, 0))
        # Etsy reports the gross the buyer paid and its charges separately, with no
        # single payout field. Net is gross minus those charges, which is the same
        # quantity eBay hands over directly — and it is what makes the profit real,
        # because tax Etsy remits is charged out again here.
        gross_cents = int((receipt.get("grandtotal") or {}).get("amount") or 0)
        payout_cents = max(0, gross_cents - fees_cents)
        # Postage the seller paid, from the label they bought for this order. This
        # is a separate out-of-pocket cost and is NOT part of the payout, which
        # already includes the shipping the BUYER was charged.
        label_cents = int((shipping_by_receipt or {}).get(int(receipt_id) if receipt_id else 0, 0))

        lines = [t for t in (receipt.get("transactions") or []) if t.get("listing_id")]
        if not lines:
            return []

        totals = [int((t.get("price") or {}).get("amount") or 0) * int(t.get("quantity") or 1)
                  for t in lines]
        basis = sum(totals) or 1

        shipping_total = int((receipt.get("total_shipping_cost") or {}).get("amount") or 0)

        sales = []
        fee_left, ship_left, payout_left = fees_cents, shipping_total, payout_cents
        label_left = label_cents
        for index, (line, line_total) in enumerate(zip(lines, totals)):
            last = index == len(lines) - 1
            if last:
                fee, ship, payout, label = fee_left, ship_left, payout_left, label_left
            else:
                share = line_total / basis
                fee = int(round(fees_cents * share))
                ship = int(round(shipping_total * share))
                payout = int(round(payout_cents * share))
                label = int(round(label_cents * share))
                fee_left -= fee
                ship_left -= ship
                payout_left -= payout
                label_left -= label

            sales.append({
                "platform_listing_id": str(line.get("listing_id")),
                "title": line.get("title") or "",
                "price_cents": line_total,
                "currency": currency,
                "sold_at": sold_at,
                "quantity": int(line.get("quantity") or 1),
                "order_id": str(receipt_id) if receipt_id else None,
                "fees_cents": max(0, fee),
                "shipping_charged_cents": max(0, ship),
                "shipping_cost_cents": max(0, label),
                "net_payout_cents": max(0, payout),
            })
        return sales

    def _normalise_results(self, results: list, limit: int = 0) -> List[dict]:
        """Map raw Etsy objects onto the Listing schema, dropping bad ones.

        Anything the normaliser cannot make sense of is skipped rather than
        passed through, because a malformed row would otherwise fail at insert
        time and be counted as a silent failure.
        """
        normalised = []
        for item in results:
            row = self._normalise_listing(item)
            if row:
                normalised.append(row)
        if limit:
            normalised = normalised[:limit]
        return normalised

    def _fetch_shop_listings(self, headers: dict, shop_id: str, limit: int) -> List[dict]:
        """Fetch this shop's own active listings.

        Endpoint: GET /v3/application/shops/{shop_id}/listings

        Only ever this endpoint. The previous version called
        /v3/application/listings/active first, which is Etsy's PUBLIC
        marketplace-wide feed: it returns listings from every seller on Etsy.
        Run against a real account it pulled in 900 pages of other people's
        listings. A sync must never read anything but the connected shop.
        """
        listings = []
        page = 1
        limit_per_page = 25
        seen_ids = set()

        while len(listings) < limit:
            try:
                resp = httpx.get(
                    f"{ETSY_API_BASE}/application/shops/{shop_id}/listings",
                    headers=headers,
                    params={
                        "page": page,
                        "limit": min(limit_per_page, limit - len(listings)),
                        # Only this shop's live listings; sold and expired ones
                        # are not inventory.
                        "state": "active",
                        # Etsy includes an "images" KEY that is null unless it is
                        # asked for. Without this the key is present but empty,
                        # which reads as "this listing has no photos" and left
                        # every imported listing with a blank image. The images
                        # then arrive inline, so this costs no extra requests.
                        "includes": "Images",
                    },
                    timeout=30,
                )
                if resp.status_code != 200:
                    self.last_error = (
                        f"Etsy returned HTTP {resp.status_code} for the shop's "
                        f"listings: {resp.text[:160]}"
                    )
                    break

                data = resp.json()
                results = data.get("results") or []
                if not results:
                    break

                # Defensive: the shop endpoint should only ever return this
                # shop's listings, but a mismatched response must not leak
                # another seller's rows into the database.
                mine = [r for r in results
                        if str((r or {}).get("shop_id", "")) == str(shop_id)]
                if len(mine) != len(results):
                    skipped = len(results) - len(mine)
                    self.last_error = (
                        f"{skipped} listing(s) were not from this shop and were "
                        f"skipped. Tell the maintainer: Etsy returned foreign rows."
                    )

                fresh = [r for r in mine
                         if str(r.get("listing_id")) not in seen_ids]
                if not fresh:
                    break  # No new rows; stop rather than loop forever.
                seen_ids.update(str(r.get("listing_id")) for r in fresh)

                listings.extend(self._normalise_results(fresh))
                if len(results) < limit_per_page:
                    break  # Last page
                page += 1
            except httpx.HTTPError as exc:
                self.last_error = f"Could not reach Etsy: {exc}"
                break

        return listings[:limit]

    def _normalise_listing(self, item: dict) -> Optional[dict]:
        """Normalise an Etsy API response into our standard Listing schema."""
        if not isinstance(item, dict):
            # Guard here rather than relying on the caller: a non-dict would
            # otherwise raise AttributeError on .get(), and at the call site
            # that surfaces as an unexplained failure count.
            return None
        try:
            # Extract price
            price_cents = 0
            price_raw = ""
            price_data = item.get("price")
            if price_data:
                if isinstance(price_data, (int, float)):
                    price_cents = int(float(price_data) * 100)
                    price_raw = f"${price_cents / 100:.2f}"
                elif isinstance(price_data, dict):
                    # Etsy v3 returns a money object:
                    #   {"amount": 598, "divisor": 100, "currency_code": "CAD"}
                    # The previous branch looked for price["value"], which is
                    # the v2 shape, so it never matched and every price came out
                    # as 0 - the amount that actually matters for profit.
                    amount = price_data.get("amount")
                    divisor = price_data.get("divisor") or 100
                    if isinstance(amount, (int, float)) and divisor:
                        price_cents = int(round(float(amount) * 100 / float(divisor)))
                        code = price_data.get("currency_code") or "USD"
                        price_raw = f"{code} {price_cents / 100:.2f}"
                    else:
                        value = price_data.get("value")
                        if isinstance(value, (int, float)):
                            price_cents = int(float(value) * 100)
                            price_raw = f"${price_cents / 100:.2f}"
                        else:
                            price_raw = str(price_data)

            # Extract images, ordered by Etsy's own rank so the primary photo is
            # stable between syncs.
            #
            # Etsy returns four sizes per image and NO plain "url" field, so the
            # previous img.get("url") always produced "" and every imported
            # listing had a blank photo. url_570xN is the sensible default:
            # large enough for the card, small enough not to waste bandwidth.
            raw_images = [i for i in (item.get("images") or []) if isinstance(i, dict)]
            raw_images.sort(key=lambda i: i.get("rank", 0) or 0)

            images = []
            for img in raw_images[:10]:
                url = (
                    img.get("url_570xN")
                    or img.get("url_fullxfull")
                    or img.get("url_170x135")
                    or img.get("url_75x75")
                    or img.get("url", "")
                )
                if url:
                    images.append(url)

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
                # Etsy reports the shop currency; this shop is CAD, so
                # hardcoding USD mislabels every amount.
                "currency": (
                    (item.get("price") or {}).get("currency_code")
                    if isinstance(item.get("price"), dict)
                    else None
                ) or "USD",
                "status": "active",
                "is_sold": item.get("is_sold", False),
                "image_url": images[0] if images else "",
                "images_json": images,
                "category": category,
                "tags": tags,
                "materials": materials,
                "available_quantity": item.get("quantity", 0),
                # views = total page views; num_favorers = hearts. Kept separate
                # because the dashboard shows each under its own label.
                "views_count": item.get("views", 0),
                "favorites_count": item.get("num_favorers", 0),
                "original_url": f"https://www.etsy.com/listing/{item.get('listing_id', '')}",
                "platform": self.PLATFORM,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            return None
