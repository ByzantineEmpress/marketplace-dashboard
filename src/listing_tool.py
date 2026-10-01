"""The multi-platform listing tool (beta).

Write an item once, publish it to several marketplaces from one action.

Most of what this module does is find out what is missing BEFORE the button is
pressed. The platforms disagree about what a listing needs: eBay wants three
business policies and an inventory location, Etsy wants a taxonomy id and a
shipping profile, and both answer a missing one with a generic validation error
that names nothing useful. A preflight that maps those onto sentences is the
difference between a tool that works and one that looks broken.

Publishing is per platform and independent: one marketplace refusing must not
stop the others, and the result says which succeeded and which did not, with each
platform's own words for why.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Platforms the tool can publish to today. The others are read-only integrations.
SUPPORTED = ("ebay", "etsy")


def _adapter(platform: str):
    from src.adapters import get_adapter

    return get_adapter(platform)


def _connected(db, user_id: int, platform: str) -> bool:
    from src.models import MarketplaceAccount

    row = (
        db.query(MarketplaceAccount)
        .filter(MarketplaceAccount.user_id == user_id,
                MarketplaceAccount.platform == platform)
        .first()
    )
    return bool(row and row.is_connected)


def _ebay_preflight(db, user_id: int) -> Dict[str, Any]:
    from src.marketplace_sync import user_credentials

    adapter = _adapter("ebay")
    credentials = user_credentials(db, user_id, "ebay")
    token = adapter.get_token(db, user_id=user_id, credentials=credentials)
    if not token:
        return {"connected": False, "ready": False,
                "problems": ["eBay is not connected, or its token has expired."],
                "options": {}}

    marketplace = adapter._resolve_marketplace(db, token, user_id) or "EBAY_US"
    prereq = adapter.account_prerequisites(token, marketplace)

    problems: List[str] = []
    if not prereq["fulfillment_policies"]:
        problems.append(
            "No eBay fulfilment policy. Create one under eBay → Account → "
            "Business policies, or the offer cannot be created.")
    if not prereq["payment_policies"]:
        problems.append("No eBay payment policy on the account.")
    if not prereq["return_policies"]:
        problems.append("No eBay return policy on the account.")
    # A location is created on demand, so its absence is not a blocker; a scope
    # failure is, because it means the location cannot be created either.
    for err in prereq["errors"]:
        if "403" in err or "401" in err:
            problems.append(
                "eBay refused to read your account settings. Reconnect the eBay "
                "account so the listing permissions are granted.")
            break

    return {
        "connected": True,
        "ready": not problems,
        "problems": problems,
        "marketplace": marketplace,
        "options": {
            "fulfillment_policies": prereq["fulfillment_policies"],
            "payment_policies": prereq["payment_policies"],
            "return_policies": prereq["return_policies"],
            "locations": prereq["locations"],
        },
    }


def _etsy_preflight(db, user_id: int) -> Dict[str, Any]:
    from src.marketplace_sync import user_credentials

    adapter = _adapter("etsy")
    credentials = user_credentials(db, user_id, "etsy")
    token = adapter.get_token(db, user_id=user_id, credentials=credentials)
    if not token:
        return {"connected": False, "ready": False,
                "problems": ["Etsy is not connected, or its token has expired."],
                "options": {}}

    shop_id = token.get("shop_id") or (token.get("token_data", {}) or {}).get("shop_id")
    if not shop_id:
        return {"connected": True, "ready": False,
                "problems": ["Could not determine your Etsy shop. Reconnect the "
                             "account so the shop permission is granted."],
                "options": {}}

    import os
    from src.adapters.base import _cred

    api_key = _cred(credentials, "api_key") or os.environ.get("ETSY_API_KEY") or ""
    api_secret = _cred(credentials, "api_secret") or os.environ.get("ETSY_API_SECRET") or ""
    headers = {
        "Authorization": f"Bearer {token['access_token']}",
        "x-api-key": f"{api_key}:{api_secret}",
    }

    options = adapter.listing_options(headers, shop_id)

    problems: List[str] = []
    if not options["shipping_profiles"]:
        problems.append("No Etsy shipping profile. Create one in your Etsy shop "
                        "settings, or the listing cannot be saved.")
    if not options["taxonomy"]:
        problems.append("Could not read Etsy's category list, which a listing must "
                        "point at.")
    for err in options["errors"]:
        if "403" in err or "401" in err:
            problems.append(
                "Etsy refused to read your shop. Reconnect the Etsy account so "
                "the listing permissions are granted.")
            break

    return {
        "connected": True,
        "ready": not problems,
        "problems": problems,
        "shop_id": shop_id,
        "options": {
            "shipping_profiles": options["shipping_profiles"],
            "taxonomy": options["taxonomy"][:400],
        },
    }


def preflight(db, user_id: int) -> Dict[str, Any]:
    """What each platform still needs before it will accept a listing."""
    platforms: Dict[str, Any] = {}
    for platform in SUPPORTED:
        if not _connected(db, user_id, platform):
            platforms[platform] = {
                "connected": False, "ready": False,
                "problems": [f"{platform.title()} is not connected."],
                "options": {},
            }
            continue
        try:
            if platform == "ebay":
                platforms[platform] = _ebay_preflight(db, user_id)
            else:
                platforms[platform] = _etsy_preflight(db, user_id)
        except Exception as exc:  # noqa: BLE001 - one platform must not hide the rest
            platforms[platform] = {
                "connected": True, "ready": False,
                "problems": [f"Could not check {platform}: {exc}"],
                "options": {},
            }
    return {
        "platforms": platforms,
        "any_ready": any(p.get("ready") for p in platforms.values()),
    }


def publish(db, user_id: int, draft: dict,
            platforms: List[str] = None) -> Dict[str, Any]:
    """Publish one draft to each requested platform, independently.

    Each platform is attempted even if an earlier one failed. A listing tool that
    stops at the first refusal would leave the seller unsure which marketplaces
    now hold the item — exactly the state this tool exists to avoid.
    """
    wanted = [p for p in (platforms or list(SUPPORTED)) if p in SUPPORTED]
    results: Dict[str, Any] = {}

    for platform in wanted:
        if not _connected(db, user_id, platform):
            results[platform] = {
                "ok": False,
                "error": f"{platform.title()} is not connected.",
            }
            continue
        try:
            from src.marketplace_sync import user_credentials

            credentials = user_credentials(db, user_id, platform)
            results[platform] = _adapter(platform).publish_listing(
                db, draft, user_id=user_id, credentials=credentials)
        except Exception as exc:  # noqa: BLE001
            results[platform] = {"ok": False, "error": str(exc)}

    published = [p for p, r in results.items() if r.get("ok")]
    return {
        "results": results,
        "published": published,
        "failed": [p for p in results if p not in published],
        "any_published": bool(published),
    }
