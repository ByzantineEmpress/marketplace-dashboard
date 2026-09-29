"""Complete, cascading deletion of user data.

Two entry points, both of which must actually ERASE rows rather than flip a
flag — a "disconnected" row still contains the person's data:

- ``delete_user_data`` — full erasure for a user of this app (the GDPR/CCPA
  right to erasure, and what the privacy policy promises). Runs when a signed-in
  user asks to delete their account.

- ``delete_ebay_connection_data`` — erasure for a matched eBay connection, run
  when eBay sends a Marketplace Account Deletion notification. eBay's data is
  spread over a ``MarketplaceAccount`` (tokens), ``Listing`` rows synced from
  eBay, and the user's eBay API keys, so all three are removed.

Deletion semantics for teams (listings are team-scoped, not user-scoped):

- A team the user shares with others is KEPT, because its listings belong to the
  whole team; only the user's membership is removed.
- A team the user is the sole member of is DELETED together with its listings,
  because those listings are exclusively theirs.
"""

from typing import Dict

from src.config import config
from src.models import (
    AuthSession,
    Listing,
    MarketplaceAccount,
    PendingSignup,
    Team,
    TeamJoinRequest,
    TeamMembership,
    User,
    UserMarketplaceCredential,
)


def delete_user_data(db, user_id: int) -> Dict[str, object]:
    """Erase a user and everything attributable to them.

    Returns a summary dict with ``"deleted": True`` and counts, or
    ``{"deleted": False, "error": ...}`` when it must refuse.
    """
    user = db.get(User, user_id)
    if not user:
        return {"deleted": False, "error": "Account not found"}

    # The bootstrap local admin is the migration's anchor (it adopts orphaned
    # listings and owns the Default Team). Deleting it would strand the
    # instance, so refuse it explicitly.
    if user.provider == "local":
        return {
            "deleted": False,
            "error": "The local administrator account cannot be deleted.",
        }

    memberships = (
        db.query(TeamMembership).filter(TeamMembership.user_id == user_id).all()
    )
    team_ids = [m.team_id for m in memberships]

    # Split teams into "solely mine" (delete whole team + listings) and "shared"
    # (keep the team and its listings, remove only my membership).
    sole_team_ids = []
    for team_id in team_ids:
        others = (
            db.query(TeamMembership)
            .filter(TeamMembership.team_id == team_id,
                    TeamMembership.user_id != user_id)
            .count()
        )
        if others == 0:
            sole_team_ids.append(team_id)

    listings_deleted = 0
    join_requests_deleted = 0

    if sole_team_ids:
        listings_deleted = (
            db.query(Listing)
            .filter(Listing.team_id.in_(sole_team_ids))
            .delete(synchronize_session=False)
        )
        join_requests_deleted += (
            db.query(TeamJoinRequest)
            .filter(TeamJoinRequest.team_id.in_(sole_team_ids))
            .delete(synchronize_session=False)
        )
        db.query(Team).filter(Team.id.in_(sole_team_ids)).delete(
            synchronize_session=False)

    memberships_deleted = (
        db.query(TeamMembership)
        .filter(TeamMembership.user_id == user_id)
        .delete(synchronize_session=False)
    )

    # Join requests the user made to others, and requests others made to them.
    join_requests_deleted += (
        db.query(TeamJoinRequest)
        .filter(TeamJoinRequest.requester_user_id == user_id)
        .delete(synchronize_session=False)
    )
    if user.email:
        join_requests_deleted += (
            db.query(TeamJoinRequest)
            .filter(TeamJoinRequest.owner_email == user.email)
            .delete(synchronize_session=False)
        )

    accounts_deleted = (
        db.query(MarketplaceAccount)
        .filter(MarketplaceAccount.user_id == user_id)
        .delete(synchronize_session=False)
    )
    credentials_deleted = (
        db.query(UserMarketplaceCredential)
        .filter(UserMarketplaceCredential.user_id == user_id)
        .delete(synchronize_session=False)
    )
    sessions_deleted = (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user_id)
        .delete(synchronize_session=False)
    )

    # A confirmed account should not have a pending signup, but erase the address
    # anyway so "delete my data" is complete regardless of where the email lives.
    if user.email:
        db.query(PendingSignup).filter(PendingSignup.email == user.email).delete(
            synchronize_session=False)

    db.delete(user)
    db.commit()

    return {
        "deleted": True,
        "listings": listings_deleted,
        "teams": len(sole_team_ids),
        "memberships": memberships_deleted,
        "marketplace_accounts": accounts_deleted,
        "credentials": credentials_deleted,
        "sessions": sessions_deleted,
        "join_requests": join_requests_deleted,
    }


def delete_ebay_connection_data(db, account: MarketplaceAccount) -> Dict[str, object]:
    """Erase one matched eBay connection and its data.

    Removes the ``MarketplaceAccount`` (tokens), the owning user's eBay API
    keys, and the eBay listings held in teams that user is the sole member of.
    Shared teams are left untouched because their eBay listings cannot be
    attributed to one connection.

    Returns counts. The caller still records the notification for audit.
    """
    user_id = account.user_id

    # eBay listings in teams this user is the sole member of. Anything else is
    # shared and stays.
    memberships = (
        db.query(TeamMembership).filter(TeamMembership.user_id == user_id).all()
        if user_id else []
    )
    sole_team_ids = []
    for m in memberships:
        others = (
            db.query(TeamMembership)
            .filter(TeamMembership.team_id == m.team_id,
                    TeamMembership.user_id != user_id)
            .count()
        )
        if others == 0:
            sole_team_ids.append(m.team_id)

    listings_deleted = 0
    if sole_team_ids:
        listings_deleted = (
            db.query(Listing)
            .filter(Listing.team_id.in_(sole_team_ids),
                    Listing.platform == "ebay")
            .delete(synchronize_session=False)
        )

    credentials_deleted = 0
    if user_id:
        credentials_deleted = (
            db.query(UserMarketplaceCredential)
            .filter(UserMarketplaceCredential.user_id == user_id,
                    UserMarketplaceCredential.platform == "ebay")
            .delete(synchronize_session=False)
        )

    db.delete(account)
    db.commit()

    return {
        "marketplace_account": 1,
        "listings": listings_deleted,
        "credentials": credentials_deleted,
    }
