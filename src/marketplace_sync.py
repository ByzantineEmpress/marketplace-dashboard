"""Running a marketplace sync, in one place.

Both the button on the dashboard and the background timer need to do exactly the
same thing — reach each platform the user has linked, pull the listings, then pull
the sales — so the sequence lives here rather than being written twice and
drifting apart.

A sync is blocking: the adapters use synchronous HTTP. Anything on the event loop
must therefore call these through ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.database import SessionLocal
from src.models import MarketplaceAccount, UserMarketplaceCredential


def user_credentials(db, user_id: int, platform: str) -> Dict[str, str]:
    """The user's decrypted credentials for one platform.

    Decrypted here and held only in memory. Values are filtered to the ones that
    are actually set, so a blank row cannot shadow a working default.
    """
    return {
        row.credential_key: row.value
        for row in db.query(UserMarketplaceCredential)
        .filter(
            UserMarketplaceCredential.user_id == user_id,
            UserMarketplaceCredential.platform == platform,
        )
        .all()
        if row.value
    }


def connected_platforms(db, user_id: int) -> List[str]:
    """Platforms this user has actually linked, in a stable order.

    Read from the connection rows rather than from the credential rows: having
    saved an API key is not the same as having authorised the account, and
    syncing a platform that was never connected would only produce an error.
    """
    rows = (
        db.query(MarketplaceAccount.platform)
        .filter(MarketplaceAccount.user_id == user_id)
        .all()
    )
    seen = []
    for (platform,) in rows:
        name = (platform or "").strip().lower()
        if name and name not in seen:
            seen.append(name)
    return sorted(seen)


def sync_platform(db, user_id: int, platform: str,
                  credentials: Dict[str, Any] = None) -> Dict[str, Any]:
    """Pull listings and then sales for one platform.

    Sales are a separate, additive pass and must run even when the listing sync
    found nothing: an account whose items have all sold has no active listings
    but plenty of sales, and every listing endpoint drops an item the moment it
    sells. A platform with no sales API reports ``supported: False`` rather than
    failing.
    """
    from src.adapters import get_adapter

    adapter = get_adapter(platform)
    if credentials is None:
        credentials = user_credentials(db, user_id, platform)

    result = adapter.sync_all(db, user_id=user_id, credentials=credentials)

    try:
        result["sales"] = adapter.sync_sales(
            db, user_id=user_id, credentials=credentials)
    except Exception as exc:  # noqa: BLE001 - one platform must not sink the rest
        result["sales"] = {"supported": True, "success": False, "fetched": 0,
                           "recorded": 0, "created": 0, "error": str(exc)}

    # A sync that reports neither error nor success is the dangerous case: it
    # looks fine on the dashboard while having changed nothing.
    if result.get("errors"):
        result["success"] = False
    return result


def sync_all_for_user(db, user_id: int) -> Dict[str, Any]:
    """Every platform this user has linked.

    One platform failing must never stop the others, so the failure is captured
    per platform and returned alongside the successes. That is what makes a
    partial result readable instead of looking like a total failure.
    """
    platforms = connected_platforms(db, user_id)
    results: Dict[str, Any] = {}
    for platform in platforms:
        try:
            results[platform] = sync_platform(db, user_id, platform)
        except Exception as exc:  # noqa: BLE001
            results[platform] = {"success": False, "error": str(exc),
                                 "listings_fetched": 0, "listings_added": 0}
    return {"platforms": platforms, "results": results}


def sync_one_for_user(user_id: int, platform: str) -> Dict[str, Any]:
    """One platform, with its own session, safe to run in a worker thread.

    A sync is blocking HTTP that can take tens of seconds. Run on the event loop
    it freezes the whole server for that long. Off it, the session must belong to
    the worker: SQLAlchemy sessions are not thread-safe, even though the SQLite
    connection underneath tolerates the thread change.
    """
    db = SessionLocal()
    try:
        return sync_platform(db, user_id, platform)
    finally:
        db.close()


def sync_all_threadsafe(user_id: int) -> Dict[str, Any]:
    """Every platform for one user, with its own session. For ``to_thread``."""
    db = SessionLocal()
    try:
        return sync_all_for_user(db, user_id)
    finally:
        db.close()


def sync_every_account() -> List[Dict[str, Any]]:
    """Sync every connected marketplace for every user.

    This is what the background timer runs, and it is deliberately not scoped to
    one user: the timer has no request and therefore no session to read a user
    from. Each account is synced with its OWN stored credentials, so this cannot
    cross one seller's data into another's.
    """
    db = SessionLocal()
    try:
        accounts = db.query(MarketplaceAccount).all()
        pairs = sorted({(a.user_id, (a.platform or "").strip().lower())
                        for a in accounts
                        if a.user_id is not None and (a.platform or "").strip()})
    finally:
        db.close()

    summary: List[Dict[str, Any]] = []
    for user_id, platform in pairs:
        db = SessionLocal()
        try:
            result = sync_platform(db, user_id, platform)
            summary.append({"user_id": user_id, "platform": platform,
                            "ok": bool(result.get("success", True)),
                            "listings_fetched": result.get("listings_fetched", 0),
                            "listings_added": result.get("listings_added", 0),
                            "sales": (result.get("sales") or {}).get("created", 0),
                            "error": result.get("error")})
        except Exception as exc:  # noqa: BLE001
            summary.append({"user_id": user_id, "platform": platform,
                            "ok": False, "error": str(exc)})
        finally:
            db.close()
    return summary
