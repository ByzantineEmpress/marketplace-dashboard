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

        # Required scopes.
        #
        # shops_r is needed to resolve the seller's shop: Etsy's token response
        # has no shop_id, and /v3/application/users/{id}/shops returns 403
        # without it. Requesting only listings_r meant the shop could never be
        # found, so every sync silently reported 0 listings.
        scopes = "listings_r shops_r"

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
