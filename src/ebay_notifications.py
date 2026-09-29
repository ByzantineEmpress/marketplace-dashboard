"""eBay Marketplace Account Deletion notifications.

eBay requires every application that uses User tokens (which this one does) to
expose an HTTPS endpoint for marketplace account deletion/closure notifications.
eBay's own wording: "Failure to comply with this requirement will result in
termination of your access to the Developer Tools, and/or reduced access to all
or some APIs." It also holds the keyset disabled until the endpoint is verified,
which is what blocked connecting an eBay account.

Two responsibilities:

1. Verification (GET). eBay sends ``?challenge_code=<random>`` and expects a
   200 with ``{"challengeResponse": "<sha256 hex>"}``, where the hash is over

       challengeCode + verificationToken + endpoint

   concatenated IN THAT ORDER. eBay's docs are explicit that the order matters
   ("in the following order or the verification will fail").

2. Notification (POST). eBay sends JSON naming the eBay user who asked for their
   data to be deleted. We must acknowledge with 200/201/202/204. The payload is
   recorded for audit and, on a best-effort match, the stored eBay connection is
   disconnected.

The verification token is a value YOU invent and register in the portal. It is
only used server-side to compute the hash, so it is never exposed by the
endpoint (a SHA-256 preimage cannot be recovered from the response).
"""

import hashlib
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The path eBay must be told to call. Kept here so the hash input and the route
# cannot drift apart.
DELETION_PATH = "/api/ebay/account-deletion"


def deletion_endpoint() -> str:
    """The public URL registered in the eBay portal.

    The hash is computed over this exact string, so it must match what is typed
    into eBay character-for-character (including any trailing slash). It is
    configurable for that reason: a proxy or a different host would otherwise
    produce hashes eBay rejects.
    """
    from src.config import config

    override = (config.EBAY_DELETION_ENDPOINT or "").strip()
    if override:
        return override
    return f"{config.APP_BASE_URL.rstrip('/')}{DELETION_PATH}"


def challenge_response(challenge_code: str, verification_token: str,
                       endpoint: Optional[str] = None) -> str:
    """SHA-256 hex of challengeCode + verificationToken + endpoint, in order.

    eBay verifies this exact concatenation, so the order is not stylistic.
    """
    if endpoint is None:
        endpoint = deletion_endpoint()
    payload = f"{challenge_code}{verification_token}{endpoint}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_notification(payload: Any) -> Dict[str, Any]:
    """Pull the fields we care about out of a notification, defensively.

    Anything malformed yields empty strings rather than raising: eBay retries on
    a non-2xx, and a payload we cannot parse is not a reason to make eBay retry
    forever. The raw payload is recorded separately for audit.
    """
    if not isinstance(payload, dict):
        return {"topic": "", "notification_id": "", "username": "",
                "user_id": "", "eias_token": "", "event_date": ""}

    metadata = payload.get("metadata") or {}
    notification = payload.get("notification") or {}
    data = notification.get("data") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(notification, dict):
        notification = {}
    if not isinstance(data, dict):
        data = {}

    def _str(value) -> str:
        return value if isinstance(value, str) else ("" if value is None else str(value))

    return {
        "topic": _str(metadata.get("topic")),
        "notification_id": _str(notification.get("notificationId")),
        "username": _str(data.get("username")),
        "user_id": _str(data.get("userId")),
        "eias_token": _str(data.get("eiasToken")),
        "event_date": _str(notification.get("eventDate")),
    }


def disconnect_matching_ebay_accounts(db, username: str, user_id: str) -> int:
    """Erase the stored data for an eBay user who deleted their account.

    Returns how many eBay connections were erased.

    Matching is by whatever eBay identity we hold on the connection: ``shop_name``
    or ``shop_id``, plus ``username``/``user_id``/``userId`` fields inside the
    raw ``token_data`` JSON (the token response may carry them). When nothing
    matches we return 0 and the caller still records the notification, so it can
    be actioned later rather than being silently dropped.

    For each match the full connection is erased via
    ``data_deletion.delete_ebay_connection_data``: the OAuth tokens, the owning
    user's eBay API keys, and their eBay listings in solely-owned teams. A flag
    flip would not be deletion, and eBay requires actual erasure.
    """
    from src.data_deletion import delete_ebay_connection_data
    from src.models import MarketplaceAccount

    if not username and not user_id:
        return 0

    query = db.query(MarketplaceAccount).filter(MarketplaceAccount.platform == "ebay")
    matches = []
    for account in query.all():
        candidates = {str(account.shop_name or ""), str(account.shop_id or "")}
        token_data = account.token_data or {}
        if isinstance(token_data, dict):
            for key in ("username", "user_id", "userId", "userID"):
                if token_data.get(key):
                    candidates.add(str(token_data[key]))

        if (username and username in candidates) or (user_id and user_id in candidates):
            matches.append(account)

    total = 0
    for account in matches:
        delete_ebay_connection_data(db, account)
        total += 1

    return total
