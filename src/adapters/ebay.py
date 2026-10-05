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
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from src.adapters.base import MarketplaceAdapter, _cred, _oauth_error
from src.database import SessionLocal
from src.models import Listing
from src.config import config

# eBay API base URL (production)
EBAY_API_BASE = "https://api.ebay.com"
# eBay serves its Commerce and Finances APIs from a SECOND host, "apiz" rather
# than "api". Calling Finances on the usual host returns an empty 404, which looks
# like a missing endpoint rather than a wrong host.
EBAY_APIZ_BASE = "https://apiz.ebay.com"

# The default category tree per marketplace. Fetched once per process: it never
# changes, and every category lookup would otherwise pay for it again.
_TAXONOMY_TREES: Dict[str, str] = {}
# eBay OAuth token endpoint
EBAY_OAUTH_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

# eBay OAuth authorisation URL
EBAY_AUTH_URL = "https://auth.ebay.com/oauth2/authorize"


def _money(value) -> int:
    """An eBay money object — {"value": "9.56", "currency": "CAD"} — in cents.

    eBay reports every amount as a decimal string, so a bare float() on the
    string gets the unit right but loses the cent: 9.56 * 100 is 955.9999...
    and truncating it would lose a penny on every fee.
    """
    if not isinstance(value, dict):
        return 0
    try:
        return int(round(float(value.get("value") or 0) * 100))
    except (TypeError, ValueError):
        return 0


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
    # The inventory report carries no titles, so listing sync generates these.
    # An order does carry the real title, so recording a sale replaces it.
    TITLE_PLACEHOLDER_PREFIX = "eBay listing "

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
        #   sell.analytics.readonly    — "View your selling analytics data". The
        #                                only source of per-listing VIEW counts:
        #                                GetItem's HitCount was deprecated by eBay,
        #                                but the Analytics traffic report still
        #                                reports LISTING_VIEWS_TOTAL.
        #   sell.fulfillment.readonly  — "View your order fulfillments". Orders are
        #                                the only source of SOLD data: every sync
        #                                reads active listings, so without this a
        #                                sale on eBay never reaches the dashboard.
        #   sell.finances              — "View and manage your payment and order
        #                                information". Orders say what the BUYER
        #                                paid for shipping, never what the seller
        #                                paid to ship it: a label bought through
        #                                eBay is a separate money movement that
        #                                only the Finances API records. Without it
        #                                every sale looks more profitable than it
        #                                was, by the cost of its label.
        #
        # Deliberately NOT requested: anything under /buy/. The Buy API (Browse)
        # would carry titles and photos too, but eBay has not granted this app any
        # buy.* scope, so it is not an option here.
        scopes = [
            "https://api.ebay.com/oauth/api_scope",
            # Read for syncing, write for the listing tool. `.readonly` alone makes
            # every create call answer 403, so the write scope is requested from
            # the start rather than discovered at publish time.
            "https://api.ebay.com/oauth/api_scope/sell.inventory",
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.listing.read",
            "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.finances",
            # Reading the seller's business policies and inventory locations,
            # without which an offer cannot be created at all.
            "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
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

    def _resolve_marketplace(self, db, token: dict, user_id=None) -> str:
        """The marketplace this seller is registered on.

        Anything marketplace-dependent needs this, and the default is not
        harmless. Asking eBay as EBAY_US when the seller is on EBAY_CA quotes
        shipping for a US buyer, so a listing that ships free within Canada comes
        back with a charge on it and is recorded as charging postage.

        The value was stored at connect time but a token refresh used to wipe it,
        so it is re-learned here and kept. Returns "" when it cannot be
        determined — callers decide what that means rather than being handed a
        guess dressed up as an answer.
        """
        marketplace = (token.get("registration_marketplace_id") or "").strip()
        if marketplace:
            return marketplace

        try:
            identity = self._fetch_identity(token.get("access_token") or "")
        except Exception:
            identity = {}

        marketplace = (identity.get("registrationMarketplaceId") or "").strip()
        if not marketplace:
            return ""

        # Keep it, so this costs one call for an old connection and none after.
        try:
            account = self._find_account(db, user_id)
            if account:
                merged = dict(account.token_data or {})
                merged["registration_marketplace_id"] = marketplace
                account.token_data = merged
                if not account.shop_id:
                    account.shop_id = identity.get("userId")
                if not account.shop_name:
                    account.shop_name = identity.get("username")
                db.commit()
        except Exception:
            db.rollback()

        return marketplace

    # -- Taxonomy: categories and their required item specifics --

    def _taxonomy_headers(self, token: dict) -> dict:
        return {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }

    def taxonomy_tree_id(self, token: dict, marketplace: str) -> str:
        """The default category tree for a marketplace.

        Cached per process because it never changes and every category lookup
        needs it; without that, choosing a category would cost two calls.
        """
        global _TAXONOMY_TREES
        if marketplace in _TAXONOMY_TREES:
            return _TAXONOMY_TREES[marketplace]
        try:
            resp = httpx.get(
                f"{EBAY_API_BASE}/commerce/taxonomy/v1/get_default_category_tree_id",
                headers=self._taxonomy_headers(token),
                params={"marketplace_id": marketplace}, timeout=30,
            )
            if resp.status_code == 200:
                tree = (resp.json() or {}).get("categoryTreeId") or ""
                if tree:
                    _TAXONOMY_TREES[marketplace] = tree
                return tree
            self.last_error = (f"eBay taxonomy tree HTTP {resp.status_code}: "
                               f"{(resp.text or '')[:160]}")
        except Exception as exc:
            self.last_error = f"eBay taxonomy tree failed: {exc}"
        return ""

    def suggest_categories(self, token: dict, marketplace: str,
                           query: str) -> List[dict]:
        """Category suggestions for a phrase.

        eBay has no browsable category list that is usable at this size, so the
        seller's own words are the way in: suggestions come back with their full
        path, which is what makes the right one identifiable.
        """
        tree = self.taxonomy_tree_id(token, marketplace)
        if not tree or not (query or "").strip():
            return []
        try:
            resp = httpx.get(
                f"{EBAY_API_BASE}/commerce/taxonomy/v1/category_tree/{tree}"
                f"/get_category_suggestions",
                headers=self._taxonomy_headers(token),
                params={"q": query[:350]}, timeout=30,
            )
            if resp.status_code != 200:
                self.last_error = (f"eBay category suggestions HTTP "
                                   f"{resp.status_code}: {(resp.text or '')[:160]}")
                return []
            out = []
            for item in (resp.json() or {}).get("categorySuggestions") or []:
                category = item.get("category") or {}
                path = " > ".join(
                    (a or {}).get("categoryName", "")
                    for a in (item.get("categoryTreeNodeAncestors") or [])
                )
                out.append({
                    "id": category.get("categoryId"),
                    "name": category.get("categoryName"),
                    "path": (path + " > " if path else "")
                            + (category.get("categoryName") or ""),
                })
            return out
        except Exception as exc:
            self.last_error = f"eBay category suggestions failed: {exc}"
            return []

    def category_aspects(self, token: dict, marketplace: str,
                         category_id: str) -> Dict[str, Any]:
        """What a category requires, so the form can ask for it.

        eBay refuses a listing whose required aspects are missing and names them
        only in the rejection. Fetching them first turns that into fields on the
        form, pre-filled where the shared fields already answer the question.
        """
        tree = self.taxonomy_tree_id(token, marketplace)
        if not tree or not category_id:
            return {"required": [], "recommended": []}
        try:
            resp = httpx.get(
                f"{EBAY_API_BASE}/commerce/taxonomy/v1/category_tree/{tree}"
                f"/get_item_aspects_for_category",
                headers=self._taxonomy_headers(token),
                params={"category_id": category_id}, timeout=30,
            )
            if resp.status_code != 200:
                self.last_error = (f"eBay aspects HTTP {resp.status_code}: "
                                   f"{(resp.text or '')[:160]}")
                return {"required": [], "recommended": []}

            required, recommended = [], []
            for aspect in (resp.json() or {}).get("aspects") or []:
                constraint = aspect.get("aspectConstraint") or {}
                name = aspect.get("localizedAspectName")
                if not name:
                    continue
                entry = {
                    "name": name,
                    "mode": constraint.get("aspectMode"),
                    "values": [
                        v.get("localizedValue")
                        for v in (aspect.get("aspectValues") or [])[:60]
                        if v.get("localizedValue")
                    ],
                }
                if constraint.get("aspectRequired"):
                    required.append(entry)
                else:
                    recommended.append(entry)
            return {"required": required, "recommended": recommended[:20]}
        except Exception as exc:
            self.last_error = f"eBay aspects failed: {exc}"
            return {"required": [], "recommended": []}

    # -- Publishing a new listing (the beta listing tool) --

    def _inventory_headers(self, token: dict, marketplace: str) -> dict:
        return {
            "Authorization": f"Bearer {token['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Required by the Inventory API. Without it every write fails with
            # "errorId 25709 Invalid value for header Content-Language", which
            # reads like a permissions problem and is not one -- it cost a round
            # of diagnosis to find.
            "Content-Language": "en-CA",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        }

    def account_prerequisites(self, token: dict, marketplace: str) -> Dict[str, Any]:
        """The seller's business policies and inventory locations.

        An offer cannot be created without all three policies and a location, and
        eBay reports a missing one as a validation error on the offer rather than
        as anything that names the real problem. Asking first turns that into a
        sentence the user can act on.
        """
        headers = self._inventory_headers(token, marketplace)
        result: Dict[str, Any] = {
            "fulfillment_policies": [], "payment_policies": [],
            "return_policies": [], "locations": [], "errors": [],
        }

        for key, path in (
            ("fulfillment_policies", "fulfillment_policy"),
            ("payment_policies", "payment_policy"),
            ("return_policies", "return_policy"),
        ):
            try:
                resp = httpx.get(
                    f"{EBAY_API_BASE}/sell/account/v1/{path}?marketplace_id={marketplace}",
                    headers=headers, timeout=30,
                )
                if resp.status_code == 200:
                    for policy in (resp.json() or {}).get(key) or []:
                        result[key].append({
                            "id": policy.get("fulfillmentPolicyId")
                                  or policy.get("paymentPolicyId")
                                  or policy.get("returnPolicyId"),
                            "name": policy.get("name"),
                        })
                else:
                    result["errors"].append(
                        f"{path}: HTTP {resp.status_code} "
                        f"{(resp.text or '')[:120]}")
            except Exception as exc:
                result["errors"].append(f"{path}: {exc}")

        try:
            resp = httpx.get(f"{EBAY_API_BASE}/sell/inventory/v1/location",
                             headers=headers, timeout=30)
            if resp.status_code == 200:
                for loc in (resp.json() or {}).get("locations") or []:
                    result["locations"].append({
                        "key": (loc.get("merchantLocationKey")
                                or (loc.get("location") or {}).get("merchantLocationKey")),
                        "name": (loc.get("name") or ""),
                    })
            else:
                result["errors"].append(
                    f"location: HTTP {resp.status_code} {(resp.text or '')[:120]}")
        except Exception as exc:
            result["errors"].append(f"location: {exc}")

        return result

    def ensure_location(self, token: dict, marketplace: str,
                        draft: dict) -> str:
        """A merchant location key, creating one if the seller has none.

        eBay will not accept an offer without one, and a seller who has only ever
        listed through the web UI may have locations without realising it, so an
        existing key is reused rather than a second one invented.
        """
        headers = self._inventory_headers(token, marketplace)
        existing = self.account_prerequisites(token, marketplace)["locations"]
        for loc in existing:
            if loc.get("key"):
                return loc["key"]

        key = "dsh-default"
        body = {
            "location": {
                "address": {
                    "country": draft.get("country") or "CA",
                    "postalCode": draft.get("postal_code") or "",
                    "city": draft.get("city") or "",
                    "stateOrProvince": draft.get("state") or "",
                }
            },
            "name": "Default location",
            "merchantLocationStatus": "ENABLED",
            "locationTypes": ["WAREHOUSE"],
        }
        resp = httpx.post(
            f"{EBAY_API_BASE}/sell/inventory/v1/location/{key}",
            headers=headers, json=body, timeout=30,
        )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"eBay would not create an inventory location: HTTP "
                f"{resp.status_code} {(resp.text or '')[:200]}")
        return key

    # eBay's condition vocabulary, keyed by the plain words the form offers. The
    # API rejects anything outside its own list, so this is a mapping rather than
    # passing the user's words through.
    CONDITION_MAP = {
        "new": "NEW",
        "like_new": "LIKE_NEW",
        "very_good": "USED_VERY_GOOD",
        "good": "USED_GOOD",
        "acceptable": "USED_ACCEPTABLE",
        "for_parts": "FOR_PARTS_OR_NOT_WORKING",
        "refurbished": "SELLER_REFURBISHED",
    }

    def publish_listing(self, db, draft: dict, user_id=None,
                        credentials: dict = None) -> Dict[str, Any]:
        """Create and publish one fixed-price listing.

        Three calls, in order, because eBay models these separately: the inventory
        item is the product, the offer is the terms, and publishing turns the
        offer into a live listing. Any one of them skipped means no listing.

        Requires sell.inventory (write), sell.account.readonly, a merchant
        location and three business policies. preflight() reports all of that up
        front; this raises with eBay's own words when something is still missing,
        because a paraphrase would be less useful than the API's message.
        """
        if not credentials:
            credentials = {}

        token = self.get_token(db, user_id=user_id, credentials=credentials)
        if not token:
            raise RuntimeError("eBay is not connected, or its token has expired.")

        marketplace = self._resolve_marketplace(db, token, user_id) or "EBAY_US"
        headers = self._inventory_headers(token, marketplace)

        sku = (draft.get("sku") or "").strip() or f"DSH-{int(time.time())}"
        quantity = max(1, int(draft.get("quantity") or 1))
        price = f"{float(draft.get('price') or 0):.2f}"
        currency = draft.get("currency") or "CAD"

        product: Dict[str, Any] = {
            "title": (draft.get("title") or "")[:80],
            "description": draft.get("description") or "",
        }
        images = [u for u in (draft.get("images") or []) if u]
        if images:
            product["imageUrls"] = images[:24]
        aspects = draft.get("aspects") or {}
        if aspects:
            # eBay wants every aspect value in a list.
            product["aspects"] = {
                str(k): (v if isinstance(v, list) else [str(v)])
                for k, v in aspects.items() if v not in (None, "")
            }

        item_body = {
            "availability": {"shipToLocationAvailability": {"quantity": quantity}},
            "condition": self.CONDITION_MAP.get(
                (draft.get("condition") or "").lower(), "USED_GOOD"),
            "product": product,
        }

        item_resp = httpx.put(
            f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item/{quote(sku, safe='')}",
            headers=headers, json=item_body, timeout=60,
        )
        if item_resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"eBay rejected the item (HTTP {item_resp.status_code}): "
                f"{(item_resp.text or '')[:300]}")

        location_key = draft.get("merchant_location_key") or self.ensure_location(
            token, marketplace, draft)

        policies = draft.get("policies") or {}
        offer_body: Dict[str, Any] = {
            "sku": sku,
            "marketplaceId": marketplace,
            "format": "FIXED_PRICE",
            "availableQuantity": quantity,
            "categoryId": str(draft.get("ebay_category_id") or ""),
            "listingDescription": draft.get("description") or "",
            "pricingSummary": {"price": {"value": price, "currency": currency}},
            "merchantLocationKey": location_key,
            "listingPolicies": {
                "fulfillmentPolicyId": policies.get("fulfillment_policy_id") or "",
                "paymentPolicyId": policies.get("payment_policy_id") or "",
                "returnPolicyId": policies.get("return_policy_id") or "",
            },
        }

        offer_resp = httpx.post(f"{EBAY_API_BASE}/sell/inventory/v1/offer",
                                headers=headers, json=offer_body, timeout=60)
        if offer_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"eBay rejected the offer (HTTP {offer_resp.status_code}): "
                f"{(offer_resp.text or '')[:300]}")
        offer_id = (offer_resp.json() or {}).get("offerId")
        if not offer_id:
            raise RuntimeError("eBay accepted the offer but returned no offer id.")

        publish_resp = httpx.post(
            f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}/publish",
            headers=headers, timeout=60,
        )
        if publish_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"eBay could not publish the offer (HTTP {publish_resp.status_code}): "
                f"{(publish_resp.text or '')[:300]}")

        listing_id = (publish_resp.json() or {}).get("listingId")
        return {
            "ok": True, "platform": self.PLATFORM,
            "listing_id": listing_id, "offer_id": offer_id, "sku": sku,
            "url": (f"https://www.ebay.ca/itm/{listing_id}" if listing_id else None),
        }

    # -- Books: create an unpublished offer (a draft) --

    # Fixed for every book this page creates, as specified.
    BOOK_PACKAGE_WEIGHT_KG = 1.0
    BOOK_PACKAGE_DIMENSIONS_CM = {"length": 25, "width": 25, "height": 10}

    # NOTE ON SHIPPING. An Inventory-API offer carries NO shipping terms: they come
    # from the seller's account settings, and this account cannot use eBay Business
    # Policies ("User is not eligible for Business Policy"), so there is nowhere to
    # attach "Canada Post Regular Parcel" and "UPS Standard Canada" per listing.
    # The weight and dimensions DO travel, on the inventory item, which is what a
    # calculated-shipping setting needs to price a quote. The services are chosen
    # once in eBay's own settings, or by hand in the draft before publishing.
    # Fallback books category per marketplace, used only when the taxonomy lookup
    # returns nothing. These are LEAF categories -- see books_category_id.
    BOOKS_LEAF_CATEGORY = {
        "EBAY_CA": "261186",   # Books & Magazines > Books
    }
    SELLER_HUB_DRAFTS = "https://www.ebay.ca/sh/lst/drafts"

    def enabled_location_key(self, token: dict, marketplace: str) -> str:
        """The seller's first enabled inventory location, or "".

        An offer needs one. It is the shipping origin that calculated shipping
        prices a quote from, and an offer without it is incomplete -- which is how
        a draft came to exist over the API while not showing up in Seller Hub.

        Read from the account rather than configured here, so no address lives in
        this codebase and the seller keeps control of what buyers are quoted from.
        """
        try:
            resp = httpx.get(f"{EBAY_API_BASE}/sell/inventory/v1/location",
                             headers=self._inventory_headers(token, marketplace),
                             timeout=40)
            if resp.status_code != 200:
                return ""
            for loc in (resp.json() or {}).get("locations") or []:
                if (loc.get("merchantLocationStatus") or "").upper() == "ENABLED":
                    return loc.get("merchantLocationKey") or ""
        except Exception:
            return ""
        return ""

    def books_category_id(self, token: dict, marketplace: str,
                          query: str = "") -> str:
        """A LEAF category to file a book under, for this marketplace.

        Being a leaf is not cosmetic, and this is the bug that hid every draft.
        eBay ACCEPTS an offer created against a non-leaf category -- 201, no
        complaint -- and then never surfaces it. The offer exists over the API and
        appears nowhere in Seller Hub, which reads to the seller as "no draft was
        created". The US parent id 267 was hardcoded here, and 267 is not a leaf:

            get_item_aspects_for_category(267) -> 400
            "The specified category ID must be a leaf category."

        261186 is the EBAY_CA books leaf. The title is tried first so the book
        lands on the right shelf, then the generic word, since suggestions are the
        only browsable way into a category tree of this size. Each candidate is
        checked for leaf-ness before it is used, because eBay's suggestions are not
        guaranteed to be leaves.
        """
        def candidates_for(phrase: str) -> List[tuple]:
            found: List[tuple] = []
            for suggestion in self.suggest_categories(token, marketplace, phrase):
                # suggest_categories reshapes eBay's reply to {id, name, path}, so
                # this reads "id" and not eBay's nested "category" object. Reading
                # the wrong key found nothing and quietly fell back every time.
                cid = str((suggestion or {}).get("id") or "").strip()
                path = str((suggestion or {}).get("path") or "")
                if cid and all(cid != c for c, _ in found):
                    found.append((cid, path))
            return found

        # This page lists BOOKS, so a suggestion that is not somewhere under Books
        # is the wrong answer however confident eBay sounds about it: the phrase
        # "Probe - leaf category check" suggested 4724, which is a real leaf
        # category and not a book at all. The title only gets to choose among
        # shelves that sit under Books; the generic word is the safety net.
        for phrase, must_be_books in (((query or "").strip(), True), ("books", False)):
            if not phrase:
                continue
            found = candidates_for(phrase)
            # Capped: each check is a round trip, and the right answer is near the
            # top of a suggestion list.
            for cid, path in found[:3]:
                if must_be_books and "book" not in path.lower():
                    continue
                try:
                    if self.category_aspects(token, marketplace, cid):
                        return cid
                except Exception:
                    continue
            if not must_be_books and found:
                return found[0][0]

        return self.BOOKS_LEAF_CATEGORY.get(marketplace, "261186")

    def create_book_draft(self, db, draft: dict, user_id=None,
                          credentials: dict = None) -> Dict[str, Any]:
        """Create an inventory item and an UNPUBLISHED offer, and return the draft.

        An unpublished offer IS an eBay draft: it appears under Seller Hub and can
        be edited and published there. Nothing goes live, which is what makes this
        safe to run and safe to test.

        Two calls, in order. The inventory item holds the product and the package,
        the offer holds the terms; the offer cannot exist without the item.

        Verified against the live account: the item is accepted with the books
        page's 1kg and 25x25x10, the offer comes back UNPUBLISHED, and neither
        needs a business policy.
        """
        if not credentials:
            credentials = {}

        token = self.get_token(db, user_id=user_id, credentials=credentials)
        if not token:
            raise RuntimeError("eBay is not connected, or its token has expired.")

        marketplace = self._resolve_marketplace(db, token, user_id) or "EBAY_US"
        headers = self._inventory_headers(token, marketplace)

        sku = (draft.get("sku") or "").strip() or f"BOOK-{int(time.time())}"

        product: Dict[str, Any] = {
            # eBay truncates the title at 80 characters, so it is cut here rather
            # than silently on their side.
            "title": (draft.get("title") or "")[:80],
            "description": draft.get("description") or "",
        }
        images = [u for u in (draft.get("images") or []) if u]
        if images:
            product["imageUrls"] = images[:24]
        aspects = draft.get("aspects") or {}
        if aspects:
            product["aspects"] = {
                str(k): (v if isinstance(v, list) else [str(v)])
                for k, v in aspects.items() if v not in (None, "")
            }

        item_body = {
            "availability": {"shipToLocationAvailability": {
                "quantity": max(1, int(draft.get("quantity") or 1))}},
            "condition": self.CONDITION_MAP.get(
                (draft.get("condition") or "").lower(), "USED_GOOD"),
            "packageWeightAndSize": {
                "weight": {"value": self.BOOK_PACKAGE_WEIGHT_KG,
                           "unit": "KILOGRAM"},
                "dimensions": {**self.BOOK_PACKAGE_DIMENSIONS_CM,
                               "unit": "CENTIMETER"},
            },
            "product": product,
        }

        item_resp = httpx.put(
            f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item/{quote(sku, safe='')}",
            headers=headers, json=item_body, timeout=60,
        )
        if item_resp.status_code not in (200, 201, 204):
            raise RuntimeError(self._inventory_error(
                "eBay rejected the book's details", item_resp))

        # A LEAF category, resolved for this marketplace. Not 267: that is the US
        # parent, and using it made eBay accept the offer and then hide it.
        category = str(draft.get("category_id") or "").strip()
        if not category:
            category = self.books_category_id(
                token, marketplace, draft.get("title") or "")

        offer_body = {
            "sku": sku,
            "marketplaceId": marketplace,
            "format": "FIXED_PRICE",
            "availableQuantity": max(1, int(draft.get("quantity") or 1)),
            "categoryId": category,
            "listingDescription": draft.get("description") or "",
            "pricingSummary": {
                "price": {"value": f"{float(draft.get('price') or 0):.2f}",
                          "currency": draft.get("currency") or "CAD"},
            },
        }
        # Business policies are added only when the seller has them. This account
        # is not eligible for them, and an offer without the container is accepted
        # -- verified -- so requiring it would break the working path.
        policies = draft.get("policies") or {}
        if any(policies.get(k) for k in
               ("fulfillment_policy_id", "payment_policy_id", "return_policy_id")):
            offer_body["listingPolicies"] = {
                "fulfillmentPolicyId": policies.get("fulfillment_policy_id") or "",
                "paymentPolicyId": policies.get("payment_policy_id") or "",
                "returnPolicyId": policies.get("return_policy_id") or "",
            }

        # The location is the shipping origin and a required part of a complete
        # offer. Without it the draft existed over the API but never appeared in
        # Seller Hub's drafts list, which read to the seller as "nothing was
        # created".
        location = self.enabled_location_key(token, marketplace)
        if location:
            offer_body["merchantLocationKey"] = location

        # NOTE: updateOffer (PUT) REPLACES the offer -- whatever is not in the body
        # is removed. Adding the location to an existing draft with a partial PUT
        # silently wiped its price, description, category and quantity. Any future
        # change to an offer must send every field back, not just the changed one.
        offer_resp = httpx.post(f"{EBAY_API_BASE}/sell/inventory/v1/offer",
                                headers=headers, json=offer_body, timeout=60)
        if offer_resp.status_code not in (200, 201):
            raise RuntimeError(self._inventory_error(
                "eBay rejected the draft offer", offer_resp))

        offer_id = (offer_resp.json() or {}).get("offerId")
        if not offer_id:
            raise RuntimeError("eBay accepted the offer but returned no offer id.")

        return {
            "ok": True,
            "platform": self.PLATFORM,
            "offer_id": offer_id,
            "sku": sku,
            "status": "UNPUBLISHED",
            "marketplace": marketplace,
            # Seller Hub has no per-draft URL, so this is the drafts list. It is
            # still one click from the draft the page just made.
            "draft_url": self.SELLER_HUB_DRAFTS,
            "note": ("Draft created. Weight and dimensions are on the item; "
                     "choose the shipping services in eBay before publishing."),
        }

    @staticmethod
    def _inventory_error(prefix: str, resp) -> str:
        """eBay's own words, plus the status.

        The Inventory API answers a missing header with an error that names a
        header and reads like a permission problem, so the raw message is worth
        more than any paraphrase this could invent.
        """
        detail = (resp.text or "").strip()[:300]
        return f"{prefix} (HTTP {resp.status_code}): {detail}"

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
            if resp.status_code >= 400:
                return {"success": False, "error": _oauth_error(resp, "eBay")}
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
            #
            # The seller's own marketplace, re-learned if an old token refresh
            # dropped it. EBAY_US is a last resort: it quotes shipping for a US
            # buyer, which misreads a Canadian listing's free domestic postage as
            # a charge.
            marketplace = self._resolve_marketplace(db, token, user_id) or "EBAY_US"
            listings = self._fetch_active_inventory_report(headers, max_results, marketplace)
            if listings:
                return self._enrich_listings(
                    listings, headers, marketplace, max_results, token.get("access_token")
                )

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
        import zipfile
        # defusedxml, not the stdlib parser: the report arrives over the network,
        # and the stdlib XML parser expands entities, so a hostile or corrupted
        # response could be an XML bomb and take the worker down. defusedxml is a
        # drop-in that rejects entities and DTDs outright.
        from defusedxml import ElementTree as ET

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
                # eBay exposes watchers (WatchCount) via the Trading API, not
                # views; filled in during enrichment, 0 until then.
                "views_count": 0,
                "watchers_count": 0,
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
                "free_shipping": self._free_shipping_from(data),
            }
        except Exception:
            return {}

    @staticmethod
    def _free_shipping_from(data: dict):
        """Whether the item ships free, from a Browse item's shipping options.

        Returns True, False, or None for "not stated". A calculated-shipping
        listing returns no shipping option at all until a buyer's location is
        known, and a missing option is not evidence that postage is charged — so
        that case stays None rather than being reported as paid shipping.
        """
        options = data.get("shippingOptions") or []
        costs = []
        for option in options:
            cost = (option or {}).get("shippingCost") or {}
            value = cost.get("value")
            if value is None:
                continue
            try:
                costs.append(float(value))
            except (TypeError, ValueError):
                continue
        if not costs:
            return None
        # Free only if every quoted option is free; a paid option anywhere means
        # the buyer can be charged.
        return all(cost == 0 for cost in costs)

    def _fetch_listing_views(self, headers: dict, marketplace: str, days: int = 30) -> dict:
        """Per-listing view counts from the Sell Analytics API traffic report.

        eBay deprecated GetItem's HitCount, so the traffic report
        (dimension=LISTING, metric=LISTING_VIEWS_TOTAL) is the only source of
        listing views. Returns {item_id: views}; empty on any failure so a
        missing scope or an empty report never breaks a sync.
        """
        from datetime import datetime, timedelta

        end = datetime.utcnow().date() - timedelta(days=1)
        start = end - timedelta(days=max(days, 1) - 1)
        params = {
            "dimension": "LISTING",
            "filter": (
                f"marketplaceid:{marketplace},"
                f"daterange:[{start}T00:00:00.000Z..{end}T23:59:59.999Z]"
            ),
            "metric": "LISTING_VIEWS_TOTAL",
            "limit": 500,
        }
        try:
            resp = httpx.get(
                f"{EBAY_API_BASE}/sell/analytics/v1/traffic_report",
                headers=headers,
                params=params,
                timeout=60,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
        except Exception:
            return {}

        views = {}
        for record in data.get("records") or []:
            if not isinstance(record, dict):
                continue

            # dimensionValues is a list of {value, dimensionKey} in the LISTING
            # report; fall back to the first value if the key name differs.
            item_id = ""
            for dim in record.get("dimensionValues") or []:
                if not isinstance(dim, dict):
                    continue
                if dim.get("dimensionKey") == "listingId" or dim.get("dimensionName") == "listingId":
                    item_id = str(dim.get("value") or "")
                    break
            if not item_id:
                dims = record.get("dimensionValues") or []
                if dims and isinstance(dims[0], dict):
                    item_id = str(dims[0].get("value") or "")
            if not item_id:
                continue

            count = 0
            for metric in record.get("metricValues") or []:
                if not isinstance(metric, dict):
                    continue
                if metric.get("metricKey") == "LISTING_VIEWS_TOTAL":
                    try:
                        count = int(float(metric.get("value") or 0))
                    except (ValueError, TypeError):
                        count = 0
            views[item_id] = count

        return views

    def sync_sales(self, db, user_id=None, credentials: dict = None,
                   days: int = 90) -> Dict[str, Any]:
        """Pull recent eBay orders and record them as sales.

        Orders are the only source of sold data on eBay. Every listing endpoint
        reads ACTIVE items, and an item leaves them the moment it sells, so
        without this a sale is invisible until someone marks it by hand.
        """
        from datetime import datetime, timedelta

        token = self.get_token(db, user_id=user_id, credentials=credentials)
        if not token:
            return {"supported": True, "success": False, "fetched": 0,
                    "recorded": 0, "created": 0,
                    "error": "No valid access token — connect the account first"}

        # Same reasoning as the listing sync: the seller's own marketplace, not a
        # US default that would quote everything for the wrong buyer.
        marketplace = self._resolve_marketplace(db, token, user_id) or "EBAY_US"
        headers = {
            "Authorization": f"Bearer {token['access_token']}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        }

        # eBay caps the window for an order query at 90 days.
        days = max(1, min(int(days), 90))
        since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        sales: List[dict] = []
        offset = 0
        page_size = 100
        try:
            while offset < 1000:
                resp = httpx.get(
                    f"{EBAY_API_BASE}/sell/fulfillment/v1/order",
                    headers=headers,
                    params={"limit": page_size, "offset": offset,
                            "filter": f"creationdate:[{since}..]"},
                    timeout=60,
                )
                if resp.status_code != 200:
                    # Report a failure only when nothing was gathered; a partial
                    # page is still worth recording.
                    if not sales:
                        detail = ""
                        try:
                            detail = resp.json()["errors"][0].get("message", "")
                        except Exception:
                            detail = resp.text[:120]
                        return {"supported": True, "success": False, "fetched": 0,
                                "recorded": 0, "created": 0,
                                "error": f"eBay orders HTTP {resp.status_code}: {detail}"}
                    break

                orders = (resp.json() or {}).get("orders") or []
                for order in orders:
                    sales.extend(self._sales_from_order(order))
                if len(orders) < page_size:
                    break
                offset += page_size
        except Exception as exc:
            if not sales:
                return {"supported": True, "success": False, "fetched": 0,
                        "recorded": 0, "created": 0,
                        "error": f"eBay orders failed: {exc}"}

        # Look the pictures up before recording, but never let that cost us the
        # sale itself: a missing photo is cosmetic, a missing sale is not.
        try:
            self._attach_sale_images(db, sales, headers, token["access_token"], marketplace)
        except Exception as exc:
            self.last_error = f"eBay sale images failed: {exc}"

        # Actual postage paid, from the payment ledger. Same reasoning: a missing
        # figure must not cost us the sale.
        try:
            shipping_by_order = self._fetch_shipping_costs(headers, since)
            self._attach_shipping_costs(sales, shipping_by_order)
        except Exception as exc:
            self.last_error = f"eBay shipping costs failed: {exc}"

        applied = self.record_sales(db, sales, owner_user_id=user_id)
        return {"supported": True, "success": True, "fetched": len(sales),
                "recorded": applied["recorded"], "created": applied["created"]}

    def _fetch_shipping_costs(self, headers: dict, since: str) -> dict:
        """What was actually paid for postage, per order, from the Finances API.

        An order records what the BUYER paid for shipping, never what the seller
        paid to ship it. A label bought through eBay is a separate money movement
        that only the Finances API reports, so without this every sale looks more
        profitable than it was by the cost of its label.

        Served from ``apiz.ebay.com`` — the Finances API is not on the usual host,
        and asking the wrong one answers with an empty 404.

        Returns ``{orderId: cents}``. Empty on failure, so a missing scope or an
        empty ledger leaves the figure at zero rather than breaking a sync.
        """
        costs: Dict[str, int] = {}
        offset = 0
        page_size = 100
        try:
            while offset < 2000:
                resp = httpx.get(
                    f"{EBAY_APIZ_BASE}/sell/finances/v1/transaction",
                    headers=headers,
                    params={"limit": page_size, "offset": offset,
                            "filter": f"transactionDate:[{since}..]"},
                    timeout=60,
                )
                if resp.status_code != 200:
                    return costs
                transactions = (resp.json() or {}).get("transactions") or []
                for txn in transactions:
                    if not isinstance(txn, dict):
                        continue
                    if (txn.get("transactionType") or "").upper() != "SHIPPING_LABEL":
                        continue
                    order_id = txn.get("orderId") or ""
                    if not order_id:
                        continue
                    # Direction is a bookingEntry, not the sign: a label is
                    # reported as a positive amount marked DEBIT. A CREDIT is a
                    # reversed or refunded label and reduces the cost. An order
                    # can carry several labels, so they accumulate.
                    amount = abs(_money(txn.get("amount")))
                    if (txn.get("bookingEntry") or "").upper() == "CREDIT":
                        costs[order_id] = costs.get(order_id, 0) - amount
                    else:
                        costs[order_id] = costs.get(order_id, 0) + amount
                if len(transactions) < page_size:
                    break
                offset += page_size
        except Exception:
            return costs
        return costs

    def _attach_shipping_costs(self, sales: List[dict], shipping_by_order: dict) -> int:
        """Spread each order's label cost across its line items.

        Split by line value with the last line taking the remainder, so the parts
        add back up to what was actually paid.
        """
        grouped: Dict[str, List[dict]] = {}
        for sale in sales:
            grouped.setdefault(sale.get("order_id") or "", []).append(sale)

        attached = 0
        for order_id, group in grouped.items():
            total = shipping_by_order.get(order_id, 0)
            if not total:
                continue
            basis = sum(s.get("price_cents") or 0 for s in group) or 1
            left = total
            for index, sale in enumerate(group):
                if index == len(group) - 1:
                    share = left
                else:
                    share = int(round(total * (sale.get("price_cents") or 0) / basis))
                    left -= share
                sale["shipping_cost_cents"] = max(0, share)
                attached += 1
        return attached

    def _attach_sale_images(self, db, sales: List[dict], browse_headers: dict,
                            access_token: str, marketplace: str) -> int:
        """Give each sale a photo, and its category.

        Orders carry neither, so each item has to be looked up. Browse answers for
        these ended listings and returns the clean s-l1600 URL; the Trading API
        GetItem is the fallback because Browse 404s for some items.

        The category comes along because the fee depends on it, and the fee
        estimate for unsold listings is built from the rates this account has
        actually been charged. A sold row with no category contributes only to the
        platform average, so capturing it here is what makes category-level
        estimates possible at all.

        Items whose picture is already stored are skipped, so a repeated sync does
        not re-fetch every sale — but a category is still filled in if that is the
        only thing missing.
        """
        import time

        if not sales:
            return 0

        ids = [s["platform_listing_id"] for s in sales if s.get("platform_listing_id")]
        known = {}
        if ids:
            known = {
                row[0]: (row[1] or "", row[2] or "")
                for row in db.query(Listing.platform_listing_id, Listing.image_url,
                                    Listing.category).filter(
                    Listing.platform == self.PLATFORM,
                    Listing.platform_listing_id.in_(ids),
                ).all()
            }

        attached = 0
        for sale in sales:
            item_id = sale.get("platform_listing_id")
            if not item_id:
                continue
            stored_image, stored_category = known.get(item_id, ("", ""))
            if stored_image:
                sale["image_url"] = sale.get("image_url") or stored_image
            if stored_category:
                sale["category"] = sale.get("category") or stored_category
                continue

            detail = self._fetch_browse_item(item_id, browse_headers)
            if detail.get("category") and not sale.get("category"):
                sale["category"] = detail["category"]

            if sale.get("image_url") or stored_image:
                # Nothing left to fetch, but the category lookup above may still
                # have been worth the call.
                if detail.get("image_url") and not stored_image:
                    sale["image_url"] = detail["image_url"]
                    sale["images"] = detail.get("images") or [detail["image_url"]]
                    attached += 1
                time.sleep(0.1)
                continue

            if detail.get("image_url"):
                sale["image_url"] = detail["image_url"]
                sale["images"] = detail.get("images") or [detail["image_url"]]
                attached += 1
                time.sleep(0.1)
                continue

            fallback = self._fetch_getitem(item_id, access_token, marketplace)
            if fallback.get("image_url"):
                sale["image_url"] = fallback["image_url"]
                sale["images"] = fallback.get("images") or [fallback["image_url"]]
                attached += 1
            time.sleep(0.1)

        return attached

    def _sales_from_order(self, order: dict) -> List[dict]:
        """One entry per order line item, shaped for record_sales().

        ``lineItemCost`` is the total for the line, so it doubles as both the
        sale price and the revenue share.

        Fees and the payout are reported per ORDER, not per item, so they are
        allocated across the line items by their share of the order. The last
        line takes the remainder rather than a rounded share, so the parts always
        add back up to the order total instead of drifting a penny at a time.
        """
        from datetime import datetime

        sold_at = None
        created = order.get("creationDate") or ""
        if created:
            try:
                sold_at = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                sold_at = None

        lines = [li for li in (order.get("lineItems") or []) if li.get("legacyItemId")]
        if not lines:
            return []

        pricing = order.get("pricingSummary") or {}
        payment = order.get("paymentSummary") or {}
        order_fee = _money(order.get("totalMarketplaceFee"))
        order_shipping = _money(pricing.get("deliveryCost"))
        order_payout = _money(payment.get("totalDueSeller"))
        currency = ((pricing.get("total") or {}).get("currency")
                    or (order.get("totalMarketplaceFee") or {}).get("currency")
                    or "CAD")

        totals = [_money(li.get("lineItemCost")) for li in lines]
        basis = sum(totals) or 1

        sales = []
        fee_left, ship_left, payout_left = order_fee, order_shipping, order_payout
        for index, (line, line_total) in enumerate(zip(lines, totals)):
            last = index == len(lines) - 1
            if last:
                fee, ship_alloc, payout = fee_left, ship_left, payout_left
            else:
                share = line_total / basis
                fee = int(round(order_fee * share))
                ship_alloc = int(round(order_shipping * share))
                payout = int(round(order_payout * share))
                fee_left -= fee
                ship_left -= ship_alloc
                payout_left -= payout

            # The line's own shipping charge is more precise than the allocated
            # share when eBay provides it.
            line_ship = _money((line.get("deliveryCost") or {}).get("shippingCost"))
            shipping_charged = line_ship if line_ship else ship_alloc

            sales.append({
                "platform_listing_id": str(line.get("legacyItemId")),
                "title": line.get("title") or "",
                "price_cents": line_total,
                "currency": currency,
                "sold_at": sold_at,
                "quantity": int(line.get("quantity") or 1),
                "order_id": order.get("orderId"),
                "fees_cents": max(0, fee),
                "shipping_charged_cents": max(0, shipping_charged),
                "net_payout_cents": max(0, payout),
            })
        return sales

    def _fetch_getitem(self, item_id: str, access_token: str, marketplace: str) -> dict:
        """Fetch an item's watcher count via the Trading API GetItem.

        eBay only exposes watchers through the legacy Trading API (not the REST
        Browse API), using the same user token in the ``X-EBAY-API-IAF-TOKEN``
        header. ``IncludeWatchCount=true`` makes it return ``WatchCount``.
        """
        # defusedxml, not the stdlib parser: see _parse_active_inventory_report.
        from defusedxml import ElementTree as ET

        site_ids = {
            "EBAY_US": "0", "EBAY_CA": "2", "EBAY_GB": "3", "EBAY_AU": "15",
            "EBAY_DE": "77", "EBAY_FR": "71", "EBAY_IT": "101",
        }
        site_id = site_ids.get(marketplace, "0")
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{item_id}</ItemID><IncludeWatchCount>true</IncludeWatchCount>"
            "</GetItemRequest>"
        )
        try:
            resp = httpx.post(
                "https://api.ebay.com/ws/api.dll",
                headers={
                    "X-EBAY-API-IAF-TOKEN": access_token,
                    "X-EBAY-API-CALL-NAME": "GetItem",
                    "X-EBAY-API-SITEID": site_id,
                    "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
                    "Content-Type": "text/xml",
                },
                content=xml,
                timeout=40,
            )
            if resp.status_code != 200:
                return {}
            root = ET.fromstring(resp.content)
            ns = "{urn:ebay:apis:eBLBaseComponents}"
            wc = root.find(f".//{ns}WatchCount")
            watchers = 0
            if wc is not None:
                try:
                    watchers = int((wc.text or "0").strip())
                except (ValueError, TypeError):
                    watchers = 0

            # Pictures come along for free in this response, which matters
            # because orders carry none and Browse 404s for some items.
            pictures = [(p.text or "").strip() for p in root.findall(f".//{ns}PictureURL")]
            pictures = [p for p in pictures if p]
            title_el = root.find(f".//{ns}Title")

            return {
                "watchers_count": watchers,
                "image_url": pictures[0] if pictures else "",
                "images": pictures,
                "title": (title_el.text or "").strip() if title_el is not None else "",
            }
        except Exception:
            return {}

    def _enrich_listings(self, rows: List[dict], headers: dict, marketplace: str,
                         limit: int, access_token: str = None) -> List[dict]:
        """Fill in title, image, category, views and watchers for each report row.

        The report alone yields generated titles and no photos. Per item, the
        Browse API supplies title/image/category/description and the Trading API
        GetItem supplies watchers. Views come from one Analytics traffic report
        call for the whole inventory rather than one call per listing. A row whose
        item cannot be fetched is left as-is, so a transient failure never drops a
        listing.
        """
        import time

        browse_headers = dict(headers)
        browse_headers["X-EBAY-C-MARKETPLACE-ID"] = marketplace

        # One call for all listings' view counts.
        views_by_item = {}
        if access_token:
            views_by_item = self._fetch_listing_views(browse_headers, marketplace)

        enriched = []
        for row in rows[:limit]:
            item_id = row["platform_listing_id"]
            detail = self._fetch_browse_item(item_id, browse_headers)
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
            # Only a successful fetch may speak about shipping — `detail` is empty
            # when the call failed, so a failure leaves the stored value alone.
            # But a fetch that succeeded and stated no shipping option means
            # exactly that, and must overwrite an older answer: several listings
            # kept a "charges postage" verdict learned from the wrong marketplace
            # long after eBay stopped quoting anything for them.
            if detail:
                row["free_shipping"] = detail.get("free_shipping")

            if views_by_item:
                row["views_count"] = views_by_item.get(item_id, 0)

            if access_token:
                watch = self._fetch_getitem(item_id, access_token, marketplace)
                row["watchers_count"] = watch.get("watchers_count", 0)

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
