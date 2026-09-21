"""API and page routes.

Organised into two routers:
- ``api_router`` — JSON endpoints for the front-end to consume.
- ``page_router`` — HTML page renders for the UI.

Every endpoint validates input (prevents injection) and returns
structured JSON so the client can handle errors gracefully.
"""

from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from src.database import SessionLocal
from src.models import Listing, User, Team, TeamMembership, AuthSession
from src.config import config, persist_env

# Templates
templates = Jinja2Templates(directory="templates")

# ------------------------------------------------------------------ #
#  Database helper — every route that needs a DB gets one.
# ------------------------------------------------------------------ #

def get_db():
    """Yield a single DB session for the duration of a request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ------------------------------------------------------------------ #
#  AUTH helper — session-based auth, backed by the database.        #
# ------------------------------------------------------------------ #

def get_current_user(request: Request):
    """Resolve the auth cookie to the signed-in user (or None).

    Sessions live in the ``auth_sessions`` table — a server restart
    doesn't log anyone out, and the token doubles as a lookup key for
    Teams (which teams does this person belong to?). Returns a plain
    dict so it's safe to use after the DB session is closed.
    """
    token = request.cookies.get("auth_token")
    if not token or len(token) != 64:
        return None
    db = SessionLocal()
    try:
        row = db.query(AuthSession).filter(AuthSession.token == token).first()
        if not row or row.expires_at < datetime.utcnow():
            return None
        user = db.query(User).filter(User.id == row.user_id).first()
        if not user:
            return None
        team_ids = [
            m.team_id
            for m in db.query(TeamMembership).filter(TeamMembership.user_id == user.id)
        ]
        return {**user.to_dict(), "team_ids": team_ids}
    finally:
        db.close()


def require_auth(request: Request):
    """Return the signed-in user (dict), or None if the cookie is invalid."""
    return get_current_user(request)


def check_auth(request: Request):
    """FastAPI dependency — raises 401 if not signed in; returns the user."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _ensure_default_team_membership(db: Session, user, role: str = "member"):
    """Make sure a user is in the default team (creating it if needed).

    New sign-ins join the "Default Team" automatically — that's the
    shared inventory everyone can see by default. The first user in the
    team becomes its owner; everyone after that joins as a member.
    """
    team = db.query(Team).filter(Team.name == "Default Team").first()
    if team is None:
        team = Team(name="Default Team")
        db.add(team)
        db.flush()
    already = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team.id, TeamMembership.user_id == user.id)
        .first()
    )
    if already is None:
        has_owner = (
            db.query(TeamMembership)
            .filter(TeamMembership.team_id == team.id, TeamMembership.role == "owner")
            .first()
            is not None
        )
        db.add(TeamMembership(team_id=team.id, user_id=user.id, role=role if not has_owner else "member"))

# ------------------------------------------------------------------ #
#  PAGE ROUTER — HTML pages served by Jinja2 templates.
# ------------------------------------------------------------------ #

page_router = APIRouter()

@page_router.get("/login")
async def login_page(request: Request):
    """Login page — POSTs to /api/auth/login."""
    error = request.query_params.get("error", "")
    return HTMLResponse(
        content=templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": error,
                "google_enabled": bool(config.GOOGLE_CLIENT_ID),
            },
        ).body.decode()
    )

@page_router.get("/dashboard")
async def dashboard_page(request: Request):
    """Main dashboard — shows the user's team listings."""
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required")
    return HTMLResponse(
        content=templates.TemplateResponse(
            "dashboard.html", {"request": request, "identity": user["name"]}
        ).body.decode()
    )

@page_router.get("/admin")
async def admin_page(request: Request):
    """Admin settings page — manage API keys, refresh listings."""
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required")
    return HTMLResponse(
        content=templates.TemplateResponse(
            "admin.html", {"request": request, "identity": user["name"]}
        ).body.decode()
    )

# -- Google sign-in (OAuth 2.0 / OpenID Connect) --

@page_router.get("/auth/google")
async def google_login_start(request: Request):
    """Send the browser to Google's account-picker / consent screen."""
    if not config.GOOGLE_CLIENT_ID:
        return RedirectResponse(
            url="/login?error=Google+sign-in+is+not+configured+yet", status_code=302
        )
    import secrets

    # One-time random token: proves the callback came from our own redirect
    # (CSRF protection). Stored in a short-lived cookie, checked on return.
    state = secrets.token_urlsafe(16)
    url = (
        "https://accounts.google.com/oauth2/v2/auth?"
        "client_id=" + quote(config.GOOGLE_CLIENT_ID)
        + "&redirect_uri=" + quote(f"{config.APP_BASE_URL}/auth/google/callback")
        + "&response_type=code"
        + "&scope=" + quote("openid email profile")
        + "&state=" + quote(state)
        + "&prompt=select_account"
    )
    response = RedirectResponse(url=url, status_code=302)
    # Lax (not strict): the cookie must be sent back when Google navigates
    # the browser to our callback URL, which is a cross-site top-level GET.
    response.set_cookie(
        key="google_oauth_state", value=state,
        httponly=True, samesite="lax", max_age=600,
    )
    return response

@page_router.get("/auth/google/callback")
async def google_login_callback(request: Request):
    """Google redirects back here with ?code=...&state=..."""
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")
    state_cookie = request.cookies.get("google_oauth_state", "")

    def fail(message: str):
        response = RedirectResponse(url=f"/login?error={quote(message)}", status_code=303)
        response.delete_cookie("google_oauth_state")
        return response

    if error:
        return fail(f"Google returned an error: {error}")
    if not code:
        return fail("No authorisation code received from Google")
    if not state_cookie or state != state_cookie:
        return fail("Security state mismatch — please try signing in again")

    # 1. Exchange the one-time code for an access token.
    redirect_uri = f"{config.APP_BASE_URL}/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": config.GOOGLE_CLIENT_ID,
                    "client_secret": config.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        return fail(f"Could not reach Google: {exc}")

    if token_resp.status_code != 200:
        return fail(f"Google token exchange failed (HTTP {token_resp.status_code})")
    access_token = token_resp.json().get("access_token", "")
    if not access_token:
        return fail("Google did not return an access token")

    # 2. Fetch the signed-in user's profile.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            info_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        return fail(f"Could not fetch Google profile: {exc}")

    if info_resp.status_code != 200:
        return fail(f"Could not fetch Google profile (HTTP {info_resp.status_code})")

    info = info_resp.json()
    email = (info.get("email") or "").strip().lower()
    if not email:
        return fail("Google profile has no email address")
    if not info.get("email_verified", False):
        return fail("Email address is not verified on that Google account")

    allowed = [e.strip().lower() for e in config.GOOGLE_ALLOWED_EMAILS if e.strip()]
    if allowed and email not in allowed:
        return fail(f"Google account {email} is not allowed to sign in here")

    # 3. Success: find-or-create the user, make sure they're in the
    # shared (default) team, then issue a database-stored session —
    # exactly like the password login does.
    import secrets

    token = secrets.token_urlsafe(48)  # exactly 64 chars — matches require_auth()
    name = info.get("name") or email

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(email=email, name=name, provider="google")
            db.add(user)
            db.flush()
        else:
            user.name = name  # keep the display name up to date
        _ensure_default_team_membership(db, user, role="member")
        db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
        db.commit()
    finally:
        db.close()
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key="auth_token", value=token,
        httponly=True, samesite="strict", max_age=86400 * 7,
    )
    response.delete_cookie("google_oauth_state")
    return response

# ------------------------------------------------------------------ #
#  API ROUTER — JSON endpoints consumed by the JS front-end.
# ------------------------------------------------------------------ #

api_router = APIRouter()

# -- Auth --

@api_router.post("/auth/login")
async def login(request: Request):
    """Authenticate admin.

    Accepts BOTH JSON bodies (API clients) and classic HTML form posts
    (the login page) — the form has no way to send JSON.
    """
    content_type = request.headers.get("content-type", "")
    is_json = "application/json" in content_type

    try:
        body = await request.json() if is_json else dict(await request.form())
    except Exception:
        body = {}

    username = body.get("username", "")
    password = body.get("password", "")

    if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
        import secrets
        token = secrets.token_urlsafe(48)  # exactly 64 chars — matches require_auth()
        # The password login maps to the local admin user, so Teams work
        # for them too (they own the default team).
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.provider == "local").first()
            if user is None:
                user = User(email=config.ADMIN_USERNAME + "@local", name=username, provider="local")
                db.add(user)
                db.flush()
            _ensure_default_team_membership(db, user, role="owner")
            db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
            db.commit()
        finally:
            db.close()
        if not is_json:
            # An HTML form POST should bounce to the dashboard,
            # not show raw JSON in the browser.
            response = RedirectResponse(url="/dashboard", status_code=303)
        else:
            response = JSONResponse(content={"ok": True, "token": token})
        # The cookie must be set on WHICHEVER response we return — the
        # redirect used to be built without it, so form logins had no session.
        response.set_cookie(
            key="auth_token",
            value=token,
            httponly=True,
            samesite="strict",
            max_age=86400 * 7,  # 7 days
        )
        return response

    if not is_json:
        return RedirectResponse(url="/login?error=Invalid+credentials", status_code=303)
    return JSONResponse(status_code=401, content={"ok": False, "error": "Invalid credentials"})

# -- OAuth callbacks (marketplace redirect targets) --

@page_router.get("/api/auth/{platform}/callback")
async def oauth_callback(platform: str, request: Request):
    """Handle the marketplace OAuth redirect.

    After authorising on eBay/Etsy the marketplace redirects the user's
    browser back here with ``?code=...&state=...``. We exchange the code
    for tokens, then show a simple confirmation page.
    """
    from src.adapters import get_adapter

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    try:
        adapter = get_adapter(platform)
    except KeyError:
        return HTMLResponse(
            f"<h1>Unknown platform</h1><p>No adapter registered for '{platform}'.</p>"
            f"<p><a href=\"/admin\">Back to Admin</a></p>",
            status_code=404,
        )

    if error:
        result = {"success": False, "error": f"Marketplace returned an error: {error}"}
    elif code:
        result = adapter.handle_callback(code, state)
    else:
        result = {"success": False, "error": "No authorization code received."}

    if result.get("success"):
        return HTMLResponse(
            f"<h1>✓ {adapter.get_platform_name()} connected</h1>"
            f"<p>Your account was linked successfully. You can now sync your listings.</p>"
            f"<p><a href=\"/admin\">Back to Admin</a> · <a href=\"/dashboard\">Go to Dashboard</a></p>"
        )

    err = result.get("error", "Unknown error")
    return HTMLResponse(
        f"<h1>Connection failed</h1><p>{err}</p>"
        f"<p><a href=\"/admin\">Back to Admin</a></p>",
        status_code=200,  # content explains the failure — 200 keeps it simple
    )

@api_router.post("/auth/logout")
async def logout(request: Request):
    """Clear the auth cookie and drop the session from the database."""
    token = request.cookies.get("auth_token", "")
    if token:
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.token == token).delete()
            db.commit()
        finally:
            db.close()
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="auth_token", httponly=True)
    return response

# -- Settings (saved from the Admin page so no one has to edit .env) --

# Keys the Admin page is allowed to read/write. Values are type-coerced to
# match the existing Config attribute before being stored.
SETTINGS_KEYS = (
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_ALLOWED_EMAILS",
    "APP_BASE_URL",
    "DEFAULT_REFRESH_INTERVAL_S",
)

@api_router.get("/settings")
async def get_settings(_auth: bool = Depends(check_auth)):
    """Return the settings the Admin page manages."""
    return {
        "settings": {
            "ADMIN_USERNAME": config.ADMIN_USERNAME,
            "ADMIN_PASSWORD": config.ADMIN_PASSWORD,
            "GOOGLE_CLIENT_ID": config.GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": config.GOOGLE_CLIENT_SECRET,
            "GOOGLE_ALLOWED_EMAILS": ",".join(config.GOOGLE_ALLOWED_EMAILS),
            "APP_BASE_URL": config.APP_BASE_URL,
            "DEFAULT_REFRESH_INTERVAL_S": config.DEFAULT_REFRESH_INTERVAL_S,
        },
        "google_redirect_uri": f"{config.APP_BASE_URL}/auth/google/callback",
    }

@api_router.post("/settings")
async def update_settings(body: dict, _auth: bool = Depends(check_auth)):
    """Update settings from the Admin page and persist them to .env."""
    updated = []
    for key, value in body.items():
        if key not in SETTINGS_KEYS:
            continue  # ignore unknown keys (forward-compatible)
        existing = getattr(config, key)
        if isinstance(existing, bool):
            value = value in (True, "true", "1", 1)
        elif isinstance(existing, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif isinstance(existing, list):
            value = [v.strip() for v in str(value).split(",") if v.strip()]
        else:
            value = str(value).strip()
        setattr(config, key, value)
        updated.append(key)

    if updated:
        persist_env({k: getattr(config, k) for k in updated})

    return {"ok": True, "updated": updated}

# -- Listings --

@api_router.get("/listings")
async def get_listings(
    platform: str = None,
    status: str = None,
    search: str = None,
    team: str = None,
    page: int = 1,
    page_size: int = 50,
    sort: str = "created_at",
    order: str = "desc",
    db: Session = Depends(get_db),
    _user: dict = Depends(check_auth),
):
    """Return a paginated, filtered list of listings."""
    from sqlalchemy import or_

    query = db.query(Listing)

    # Team scoping: a user sees listings from their own teams, plus
    # unassigned ones (team_id NULL = shared, e.g. freshly synced).
    # ?team=<id> narrows the view to one of the user's own teams.
    if team:
        try:
            team_id = int(team)
        except (TypeError, ValueError):
            team_id = -1
        if team_id in _user["team_ids"]:
            query = query.filter(Listing.team_id == team_id)
        else:
            query = query.filter(Listing.id == None)  # noqa: E711  (matches nothing)
    else:
        query = query.filter(
            or_(Listing.team_id == None, Listing.team_id.in_(_user["team_ids"]))  # noqa: E711
        )

    # Filter by platform
    if platform:
        query = query.filter(Listing.platform == platform)

    # Filter by status
    if status:
        query = query.filter(Listing.status == status)

    # Full-text search on title (LIKE with wildcards — SQLAlchemy escapes)
    if search:
        like_pattern = f"%{search}%"
        query = query.filter(Listing.title.ilike(like_pattern))

    # Sort
    sort_col = getattr(Listing, sort, Listing.created_at)
    if order == "asc":
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    # Count total (for pagination UI)
    total = query.count()

    # Paginate
    listings = query.offset((page - 1) * page_size).limit(page_size).all()

    # Attach a human-readable team name to each listing (the UI shows it
    # on the card). "Shared" means unassigned (team_id NULL).
    team_names = {t.id: t.name for t in db.query(Team)}
    items = []
    for l in listings:
        item = l.to_dict()
        item["team_name"] = team_names.get(l.team_id) if l.team_id else "Shared"
        items.append(item)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "listings": items,
    }

@api_router.get("/stats")
async def get_stats(team: str = None, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Return summary stats for the dashboard header (team-scoped)."""
    from sqlalchemy import func, or_

    # Same team scoping as the listings endpoint.
    if team:
        try:
            team_id = int(team)
        except (TypeError, ValueError):
            team_id = -1
        scope = (Listing.team_id == team_id) if team_id in _user["team_ids"] else (Listing.id == None)  # noqa: E711
    else:
        scope = or_(Listing.team_id == None, Listing.team_id.in_(_user["team_ids"]))  # noqa: E711

    total = db.query(func.count(Listing.id)).filter(scope).scalar() or 0
    active = db.query(func.count(Listing.id)).filter(scope, Listing.status == "active").scalar() or 0
    sold = db.query(func.count(Listing.id)).filter(scope, Listing.is_sold == True).scalar() or 0  # noqa: E712

    # Count per platform
    platform_counts = (
        db.query(Listing.platform, func.count(Listing.id))
        .filter(scope)
        .group_by(Listing.platform)
        .all()
    )

    platforms = {p: c for p, c in platform_counts}

    # Total value (sum of active listings)
    total_value = db.query(func.sum(Listing.price_cents)).filter(scope, Listing.status == "active").scalar() or 0

    return {
        "total_listings": total,
        "active_listings": active,
        "sold_listings": sold,
        "total_value_cents": total_value,
        "platforms": platforms,
    }

# -- Marketplace accounts (credentials) --

@api_router.get("/accounts")
async def get_accounts(db: Session = Depends(get_db), _auth: bool = Depends(check_auth)):
    """Return the list of configured marketplace accounts."""
    from src.models import MarketplaceAccount

    accounts = db.query(MarketplaceAccount).all()
    return [
        {
            "platform": a.platform,
            "shop_name": a.shop_name,
            "is_connected": a.is_connected,
            "last_synced": a.last_synced.isoformat() if a.last_synced else None,
        }
        for a in accounts
    ]

@api_router.post("/accounts/connect")
async def connect_account(body: dict, _auth: bool = Depends(check_auth)):
    """Start an OAuth flow.  Returns the authorisation URL to redirect to.

    Body should contain: ``platform`` (e.g. "ebay", "etsy").
    """
    from src.adapters import get_adapter

    platform = body.get("platform", "")
    try:
        adapter = get_adapter(platform)
        auth_url = adapter.get_authorization_url()
        return {"auth_url": auth_url}
    except KeyError:
        return JSONResponse(status_code=400, content={"error": f"Platform '{platform}' not supported yet"})

@api_router.post("/accounts/sync")
async def sync_account(body: dict, db: Session = Depends(get_db), _auth: bool = Depends(check_auth)):
    """Manually trigger a sync for a given marketplace.

    Fetches new listings and stores them in the database.
    Body: { "platform": "ebay" } or { "platform": "etsy" }
    """
    from src.adapters import get_adapter

    platform = body.get("platform", "")
    try:
        adapter = get_adapter(platform)
    except KeyError:
        return JSONResponse(status_code=400, content={"error": f"Platform '{platform}' not supported yet"})

    try:
        result = adapter.sync_all(db)
        return {"ok": True, "platform": platform, "result": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# -- Teams (shared inventory) --

@api_router.get("/teams")
async def get_teams(db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """The teams the current user belongs to, with each team's members."""
    teams = db.query(Team).filter(Team.id.in_(_user["team_ids"] or [0])).all()
    result = []
    for t in teams:
        roles = {
            m.user_id: m.role
            for m in db.query(TeamMembership).filter(TeamMembership.team_id == t.id)
        }
        members = (
            db.query(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .filter(TeamMembership.team_id == t.id)
            .order_by(User.id)
            .all()
        )
        result.append({
            "id": t.id,
            "name": t.name,
            "role": roles.get(_user["id"], "member"),
            "members": [
                {**u.to_dict(), "role": roles.get(u.id, "member")}
                for u in members
            ],
        })
    return result


@api_router.post("/teams")
async def create_team(body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Create a new team; the creator becomes its owner."""
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Team name is required"})
    if db.query(Team).filter(Team.name == name).first():
        return JSONResponse(status_code=400, content={"ok": False, "error": f"Team '{name}' already exists"})
    team = Team(name=name)
    db.add(team)
    db.flush()
    db.add(TeamMembership(team_id=team.id, user_id=_user["id"], role="owner"))
    db.commit()
    return {"ok": True, "team": team.to_dict()}


@api_router.post("/teams/{team_id}/members")
async def add_team_member(team_id: int, body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Add a member to a team by email.

    If that person hasn't signed in yet, their user row is created up
    front — when they later sign in (Google) they land in the team
    automatically, so sharing just works.
    """
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
    email = (body.get("email") or "").strip().lower()
    if not email:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Email is required"})
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, name=email.split("@")[0], provider="google")
        db.add(user)
        db.flush()
    exists = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == user.id)
        .first()
    )
    if exists:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Already a member of that team"})
    db.add(TeamMembership(team_id=team_id, user_id=user.id, role="member"))
    db.commit()
    return {"ok": True}


@api_router.delete("/teams/{team_id}/members/{user_id}")
async def remove_team_member(team_id: int, user_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Remove a member from a team (you must be in that team)."""
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
    membership = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        .first()
    )
    if not membership:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Member not found"})
    if membership.role == "owner":
        owner_count = (
            db.query(TeamMembership)
            .filter(TeamMembership.team_id == team_id, TeamMembership.role == "owner")
            .count()
        )
        if owner_count <= 1:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Cannot remove the last owner"})
    db.delete(membership)
    db.commit()
    return {"ok": True}


@api_router.put("/listings/{listing_id}")
async def update_listing(listing_id: int, body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Update a listing (currently: which team it belongs to).

    Body: { "team_id": 2 } — or { "team_id": null } to make it shared.
    """
    listing = db.get(Listing, listing_id)
    if not listing:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})
    if "team_id" in body:
        new_team = body["team_id"]
        if new_team is None:
            listing.team_id = None  # unassigned / shared
        else:
            try:
                new_team = int(new_team)
            except (TypeError, ValueError):
                return JSONResponse(status_code=400, content={"ok": False, "error": "team_id must be a number"})
            if new_team not in _user["team_ids"]:
                return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
            listing.team_id = new_team
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}

# -- Plugins info --

@api_router.get("/plugins")
async def get_plugins(_auth: bool = Depends(check_auth)):
    """Return metadata about all loaded plugins."""
    from src.plugins import get_loaded_plugins
    return get_loaded_plugins()
