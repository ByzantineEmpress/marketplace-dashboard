"""API and page routes.

Organised into two routers:
- ``api_router`` — JSON endpoints for the front-end to consume.
- ``page_router`` — HTML page renders for the UI.

Every endpoint validates input (prevents injection) and returns
structured JSON so the client can handle errors gracefully.
"""

from datetime import datetime, timedelta
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File
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


def check_admin(request: Request):
    """FastAPI dependency — raises 403 if user lacks admin privileges; returns user."""
    user = check_auth(request)
    is_admin = (
        user.get("provider") == "local"
        or user.get("email") == f"{config.ADMIN_USERNAME}@local"
        or user.get("is_admin") is True
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def _ensure_admin_team_membership(db: Session, user):
    """Make sure the local administrator has an owned team."""
    team = db.query(Team).filter(Team.name == "Default Team").first()
    if team is None:
        import secrets
        team = Team(name="Default Team", invite_code=secrets.token_urlsafe(16))
        db.add(team)
        db.flush()
    already = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team.id, TeamMembership.user_id == user.id)
        .first()
    )
    if already is None:
        db.add(TeamMembership(team_id=team.id, user_id=user.id, role="owner"))


def _process_pending_invite(db: Session, user, invite_code: str):
    """If an invite_code is provided, add user to that team as a member."""
    if not invite_code:
        return None
    team = db.query(Team).filter(Team.invite_code == invite_code).first()
    if team:
        exists = db.query(TeamMembership).filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == user.id,
        ).first()
        if not exists:
            db.add(TeamMembership(team_id=team.id, user_id=user.id, role="member"))
            db.flush()
        return team
    return None


def _ensure_user_tenant_membership(db: Session, user, pending_invite: str = ""):
    """Ensure a user has an isolated workspace/team.

    1. If user has a valid pending invite code, join that invited team as a member.
    2. If the user already belongs to at least one team, keep their existing teams.
    3. If the user belongs to no teams and has no invite, create a dedicated
       personal tenant workspace (e.g. "{User's Name}'s Workspace"), where they
       are the sole OWNER.

    This guarantees strict tenant isolation: no new user ever gets dumped
    into another tenant's or the global admin's team!
    """
    if pending_invite:
        joined_team = _process_pending_invite(db, user, pending_invite)
        if joined_team:
            return joined_team

    # Check if user already belongs to any team
    existing_membership = db.query(TeamMembership).filter(TeamMembership.user_id == user.id).first()
    if existing_membership:
        return db.get(Team, existing_membership.team_id)

    # Brand-new tenant: create their own private workspace with a unique name
    import secrets
    base_name = f"{user.name}'s Workspace" if user.name else "My Workspace"
    ws_name = base_name
    counter = 1
    while db.query(Team).filter(Team.name == ws_name).first():
        ws_name = f"{base_name} ({secrets.token_hex(2)})"
        counter += 1
        if counter > 5:
            ws_name = f"{base_name} {secrets.token_hex(4)}"
            break

    new_team = Team(name=ws_name, invite_code=secrets.token_urlsafe(16))
    db.add(new_team)
    db.flush()
    db.add(TeamMembership(team_id=new_team.id, user_id=user.id, role="owner"))
    return new_team

# ------------------------------------------------------------------ #
#  PAGE ROUTER — HTML pages served by Jinja2 templates.
# ------------------------------------------------------------------ #

page_router = APIRouter()

@page_router.get("/login")
async def login_page(request: Request):
    """Login page — POSTs to /api/auth/login."""
    error = request.query_params.get("error", "")
    invited_to = request.query_params.get("invited_to", "")
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "invited_to": invited_to,
            "google_enabled": bool(config.GOOGLE_CLIENT_ID or config.GOOGLE_DEV_MODE),
        },
    )


@page_router.get("/join/{invite_code}")
async def join_team_page(invite_code: str, request: Request, db: Session = Depends(get_db)):
    """Accept a shareable team invite link."""
    team = db.query(Team).filter(Team.invite_code == invite_code).first()
    if not team:
        return RedirectResponse(url="/login?error=Invalid+or+expired+team+invite+link", status_code=302)

    current_user = get_current_user(request)
    if current_user:
        exists = db.query(TeamMembership).filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == current_user["id"],
        ).first()
        if not exists:
            db.add(TeamMembership(team_id=team.id, user_id=current_user["id"], role="member"))
            db.commit()
        return RedirectResponse(url=f"/dashboard?team={team.id}", status_code=302)

    # User is not logged in: store invite in cookie and prompt sign in
    response = RedirectResponse(url=f"/login?invited_to={quote(team.name)}", status_code=302)
    response.set_cookie(key="pending_invite", value=invite_code, httponly=True, max_age=3600)
    return response

@page_router.get("/dashboard")
async def dashboard_page(request: Request):
    """Main dashboard — shows the user's team listings."""
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"identity": user["name"]},
    )

@page_router.get("/admin")
async def admin_page(request: Request):
    """Admin settings page — manage API keys, refresh listings."""
    user = require_auth(request)
    if not user:
        return RedirectResponse(url="/login?error=auth_required")
    is_admin = (
        user.get("provider") == "local"
        or user.get("email") == f"{config.ADMIN_USERNAME}@local"
        or user.get("is_admin") is True
    )
    if not is_admin:
        return RedirectResponse(url="/dashboard?error=admin_privileges_required")
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"identity": user["name"]},
    )

# -- Google sign-in (OAuth 2.0 / OpenID Connect) --

@page_router.get("/auth/google")
async def google_login_start(request: Request):
    """Send the browser to Google's account-picker / consent screen."""
    force_live = request.query_params.get("live") == "1"
    force_dev = request.query_params.get("dev") == "1"

    if config.GOOGLE_DEV_MODE:
        if force_dev or not force_live:
            return RedirectResponse(url="/auth/google/dev-picker", status_code=302)
    elif force_dev:
        return RedirectResponse(url="/login?error=Google+dev+mode+is+disabled", status_code=303)

    if not config.GOOGLE_CLIENT_ID:
        return RedirectResponse(
            url="/login?error=Google+sign-in+is+not+configured+yet", status_code=302
        )

    if not force_live and "dummy" in config.GOOGLE_CLIENT_ID.lower():
        if config.GOOGLE_DEV_MODE:
            return RedirectResponse(url="/auth/google/dev-picker", status_code=302)
        return RedirectResponse(url="/login?error=Google+client+id+not+configured", status_code=303)
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
        secure=config.REQUIRE_HTTPS,
    )
    return response


@page_router.get("/auth/google/dev-picker")
async def google_dev_picker(request: Request):
    """Local development/testing Google Account picker."""
    if not config.GOOGLE_DEV_MODE:
        raise HTTPException(status_code=403, detail="Google dev mode is disabled")
    error = request.query_params.get("error", "")
    return templates.TemplateResponse(
        request=request,
        name="google_dev_picker.html",
        context={"error": error},
    )


@page_router.post("/auth/google/dev-login")
async def google_dev_login(request: Request):
    """Simulate Google OAuth callback for local development."""
    if not config.GOOGLE_DEV_MODE:
        raise HTTPException(status_code=403, detail="Google dev mode is disabled")
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    name = (form.get("name") or "").strip() or email.split("@")[0].capitalize()

    if not email or "@" not in email:
        return RedirectResponse(url="/auth/google/dev-picker?error=Please+provide+a+valid+email", status_code=303)

    allowed = [e.strip().lower() for e in config.GOOGLE_ALLOWED_EMAILS if e.strip()]
    if allowed and email not in allowed:
        return RedirectResponse(
            url=f"/auth/google/dev-picker?error=Google+account+{quote(email)}+is+not+allowed+to+sign+in+here",
            status_code=303,
        )

    import secrets
    token = secrets.token_urlsafe(48)  # 64 chars

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(email=email, name=name, provider="google")
            db.add(user)
            db.flush()
        else:
            if name:
                user.name = name
        pending_invite = request.cookies.get("pending_invite", "")
        joined_team = _ensure_user_tenant_membership(db, user, pending_invite)
        joined_team_id = joined_team.id if joined_team else None
        db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
        db.commit()
    finally:
        db.close()

    dest = f"/dashboard?team={joined_team_id}" if (pending_invite and joined_team_id) else "/dashboard"
    response = RedirectResponse(url=dest, status_code=303)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="strict",
        max_age=86400 * 7,
    )
    if request.cookies.get("pending_invite"):
        response.delete_cookie("pending_invite")
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
        pending_invite = request.cookies.get("pending_invite", "")
        joined_team = _ensure_user_tenant_membership(db, user, pending_invite)
        joined_team_id = joined_team.id if joined_team else None
        db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
        db.commit()
    finally:
        db.close()
    dest = f"/dashboard?team={joined_team_id}" if (pending_invite and joined_team_id) else "/dashboard"
    response = RedirectResponse(url=dest, status_code=303)
    response.set_cookie(
        key="auth_token", value=token,
        httponly=True, samesite="strict", max_age=86400 * 7,
    )
    response.delete_cookie("google_oauth_state")
    if request.cookies.get("pending_invite"):
        response.delete_cookie("pending_invite")
    return response

# ------------------------------------------------------------------ #
#  API ROUTER — JSON endpoints consumed by the JS front-end.
# ------------------------------------------------------------------ #

api_router = APIRouter()

# -- Auth --

LOGIN_ATTEMPTS = {}  # ip -> list of timestamps
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = LOGIN_ATTEMPTS.get(ip, [])
    valid = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[ip] = valid
    return len(valid) >= LOGIN_MAX_ATTEMPTS

def _record_failed_attempt(ip: str):
    now = time.time()
    attempts = LOGIN_ATTEMPTS.get(ip, [])
    attempts.append(now)
    LOGIN_ATTEMPTS[ip] = attempts

@api_router.post("/auth/login")
async def login(request: Request):
    """Authenticate admin.

    Accepts BOTH JSON bodies (API clients) and classic HTML form posts
    (the login page) — the form has no way to send JSON.
    Protected with rate limiting and constant-time password check.
    """
    client_ip = request.client.host if request.client else "unknown"
    content_type = request.headers.get("content-type", "")
    is_json = "application/json" in content_type

    if _is_rate_limited(client_ip):
        if not is_json:
            return RedirectResponse(url="/login?error=Too+many+failed+attempts.+Please+wait+60+seconds", status_code=303)
        return JSONResponse(status_code=429, content={"ok": False, "error": "Too many failed login attempts. Please wait 60 seconds."})

    try:
        body = await request.json() if is_json else dict(await request.form())
    except Exception:
        body = {}

    username = str(body.get("username", ""))
    password = str(body.get("password", ""))

    import hmac
    valid_user = hmac.compare_digest(username.strip(), config.ADMIN_USERNAME.strip())
    valid_pass = hmac.compare_digest(password, config.ADMIN_PASSWORD)

    if valid_user and valid_pass:
        LOGIN_ATTEMPTS.pop(client_ip, None)
        import secrets
        token = secrets.token_urlsafe(48)  # exactly 64 chars — matches require_auth()
        # The password login maps to the local admin user, so Teams work
        # for them too (they own the default team).
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.provider == "local").first()
            if user is None:
                user = User(email=config.ADMIN_USERNAME + "@local", name=username, provider="local", is_admin=True)
                db.add(user)
                db.flush()
            else:
                user.is_admin = True
            _ensure_admin_team_membership(db, user)
            pending_invite = request.cookies.get("pending_invite", "")
            joined_team = _process_pending_invite(db, user, pending_invite)
            joined_team_id = joined_team.id if joined_team else None
            db.add(AuthSession(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(days=7)))
            db.commit()
        finally:
            db.close()
        dest = f"/dashboard?team={joined_team_id}" if joined_team_id else "/dashboard"
        if not is_json:
            response = RedirectResponse(url=dest, status_code=303)
        else:
            response = JSONResponse(content={"ok": True, "token": token})
        response.set_cookie(
            key="auth_token",
            value=token,
            httponly=True,
            samesite="strict",
            max_age=86400 * 7,  # 7 days
            secure=config.REQUIRE_HTTPS,
        )
        if request.cookies.get("pending_invite"):
            response.delete_cookie("pending_invite")
        return response

    _record_failed_attempt(client_ip)
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
    "GOOGLE_DEV_MODE",
    "APP_BASE_URL",
    "DEFAULT_REFRESH_INTERVAL_S",
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
    "ETSY_API_KEY",
    "ETSY_API_SECRET",
    "DEFAULT_CURRENCY",
    "DEFAULT_CURRENCY_SYMBOL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_USE_TLS",
    "POSHMARK_USERNAME",
    "POSHMARK_API_KEY",
    "AMAZON_SELLER_ID",
    "AMAZON_CLIENT_ID",
    "AMAZON_CLIENT_SECRET",
    "AMAZON_REFRESH_TOKEN",
    "AMAZON_MARKETPLACE_ID",
)

SECRET_KEYS = {
    "ADMIN_PASSWORD",
    "GOOGLE_CLIENT_SECRET",
    "SMTP_PASSWORD",
    "EBAY_CLIENT_SECRET",
    "ETSY_API_SECRET",
    "POSHMARK_API_KEY",
    "AMAZON_CLIENT_SECRET",
    "AMAZON_REFRESH_TOKEN",
}

def _mask_secret(val: str) -> str:
    return "••••••••" if val else ""

@api_router.get("/settings")
async def get_settings(_admin: dict = Depends(check_admin)):
    """Return the settings the Admin page manages (requires admin privileges)."""
    return {
        "settings": {
            "ADMIN_USERNAME": config.ADMIN_USERNAME,
            "ADMIN_PASSWORD": _mask_secret(config.ADMIN_PASSWORD),
            "GOOGLE_CLIENT_ID": config.GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": _mask_secret(config.GOOGLE_CLIENT_SECRET),
            "GOOGLE_ALLOWED_EMAILS": ",".join(config.GOOGLE_ALLOWED_EMAILS),
            "GOOGLE_DEV_MODE": config.GOOGLE_DEV_MODE,
            "APP_BASE_URL": config.APP_BASE_URL,
            "DEFAULT_CURRENCY": config.DEFAULT_CURRENCY,
            "DEFAULT_CURRENCY_SYMBOL": config.DEFAULT_CURRENCY_SYMBOL,
            "SMTP_HOST": config.SMTP_HOST,
            "SMTP_PORT": config.SMTP_PORT,
            "SMTP_USER": config.SMTP_USER,
            "SMTP_PASSWORD": _mask_secret(config.SMTP_PASSWORD),
            "SMTP_FROM": config.SMTP_FROM,
            "SMTP_USE_TLS": config.SMTP_USE_TLS,
            "DEFAULT_REFRESH_INTERVAL_S": config.DEFAULT_REFRESH_INTERVAL_S,
            "EBAY_CLIENT_ID": config.EBAY_CLIENT_ID,
            "EBAY_CLIENT_SECRET": _mask_secret(config.EBAY_CLIENT_SECRET),
            "ETSY_API_KEY": config.ETSY_API_KEY,
            "ETSY_API_SECRET": _mask_secret(config.ETSY_API_SECRET),
            "POSHMARK_USERNAME": config.POSHMARK_USERNAME,
            "POSHMARK_API_KEY": _mask_secret(config.POSHMARK_API_KEY),
            "AMAZON_SELLER_ID": config.AMAZON_SELLER_ID,
            "AMAZON_CLIENT_ID": config.AMAZON_CLIENT_ID,
            "AMAZON_CLIENT_SECRET": _mask_secret(config.AMAZON_CLIENT_SECRET),
            "AMAZON_REFRESH_TOKEN": _mask_secret(config.AMAZON_REFRESH_TOKEN),
            "AMAZON_MARKETPLACE_ID": config.AMAZON_MARKETPLACE_ID,
        },
        "google_redirect_uri": f"{config.APP_BASE_URL}/auth/google/callback",
        "ebay_redirect_uri": f"{config.APP_BASE_URL}/api/auth/ebay/callback",
        "etsy_redirect_uri": f"{config.APP_BASE_URL}/api/auth/etsy/callback",
        "poshmark_redirect_uri": f"{config.APP_BASE_URL}/api/auth/poshmark/callback",
        "amazon_redirect_uri": f"{config.APP_BASE_URL}/api/auth/amazon/callback",
    }

@api_router.post("/settings")
async def update_settings(body: dict, _admin: dict = Depends(check_admin)):
    """Update settings from the Admin page and persist them to .env (admin only)."""
    updated = []
    for key, value in body.items():
        if key not in SETTINGS_KEYS:
            continue  # ignore unknown keys (forward-compatible)
        # Never overwrite sensitive secret fields if masked with dots or empty
        if key in SECRET_KEYS:
            val_str = str(value).strip()
            if not val_str or "••" in val_str or "****" in val_str:
                continue

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
        if key == "EBAY_CLIENT_ID": config.EBUY_CLIENT_ID = value
        elif key == "EBAY_CLIENT_SECRET": config.EBUY_CLIENT_SECRET = value
        elif key == "ETSY_API_KEY": config.ESY_API_KEY = value
        elif key == "ETSY_API_SECRET": config.ESY_API_SECRET = value
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

    # Team scoping: strictly isolated to caller's verified teams.
    # A user only ever sees listings belonging to their own teams.
    user_team_ids = _user.get("team_ids") or []
    # For local admin, auto-claim any legacy unassigned listings into the admin's team
    if (_user.get("is_admin") or _user.get("provider") == "local") and user_team_ids:
        unassigned = db.query(Listing).filter(Listing.team_id == None).all()
        if unassigned:
            for item in unassigned:
                item.team_id = user_team_ids[0]
            db.commit()

    if not user_team_ids:
        query = query.filter(Listing.id == None)  # Matches nothing
    elif team:
        try:
            team_id = int(team)
        except (TypeError, ValueError):
            team_id = -1
        if team_id in user_team_ids:
            query = query.filter(Listing.team_id == team_id)
        else:
            query = query.filter(Listing.id == None)  # Matches nothing
    else:
        query = query.filter(Listing.team_id.in_(user_team_ids))

    # Filter by platform (primary platform or cross-listed in platforms_json)
    if platform:
        query = query.filter(
            or_(
                Listing.platform == platform,
                Listing.platforms_json.like(f'%"{platform}"%'),
            )
        )

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

    # Attach team name from caller's teams only (prevents leaking other tenant team names)
    team_names = {t.id: t.name for t in db.query(Team).filter(Team.id.in_(user_team_ids)).all()}
    items = []
    for l in listings:
        item = l.to_dict()
        item["team_name"] = team_names.get(l.team_id) if l.team_id else "Unassigned"
        items.append(item)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "listings": items,
    }

@api_router.get("/stats")
async def get_stats(team: str = None, days: str = None, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Return summary stats for the dashboard header (team-scoped, optional time-filtered)."""
    from sqlalchemy import func, or_, and_
    from sqlalchemy.sql.functions import coalesce

    # Same strict team scoping as the listings endpoint.
    user_team_ids = _user.get("team_ids") or []
    # For local admin, auto-claim any legacy unassigned listings into the admin's team
    if (_user.get("is_admin") or _user.get("provider") == "local") and user_team_ids:
        unassigned = db.query(Listing).filter(Listing.team_id == None).all()
        if unassigned:
            for item in unassigned:
                item.team_id = user_team_ids[0]
            db.commit()

    if not user_team_ids:
        scope = (Listing.id == None)
    elif team:
        try:
            team_id = int(team)
        except (TypeError, ValueError):
            team_id = -1
        scope = (Listing.team_id == team_id) if team_id in user_team_ids else (Listing.id == None)
    else:
        scope = Listing.team_id.in_(user_team_ids)

    days_int = None
    if days and str(days).lower() not in ("all", "0", "none", ""):
        try:
            days_int = int(days)
        except (TypeError, ValueError):
            days_int = None

    cutoff = (datetime.utcnow() - timedelta(days=days_int)) if days_int else None

    if cutoff:
        sold_time = coalesce(Listing.sold_at, Listing.updated_at)
        sold_scope = and_(scope, Listing.is_sold == True, sold_time >= cutoff)  # noqa: E712
        active_scope = and_(scope, Listing.status == "active", Listing.created_at >= cutoff)
        total_scope = and_(scope, Listing.created_at >= cutoff)
        write_off_time = coalesce(Listing.written_off_at, Listing.updated_at)
        written_off_scope = and_(scope, Listing.status == "written_off", write_off_time >= cutoff)
    else:
        sold_scope = and_(scope, Listing.is_sold == True)  # noqa: E712
        active_scope = and_(scope, Listing.status == "active")
        total_scope = scope
        written_off_scope = and_(scope, Listing.status == "written_off")

    total = db.query(func.count(Listing.id)).filter(total_scope).scalar() or 0
    active = db.query(func.count(Listing.id)).filter(active_scope).scalar() or 0
    sold = db.query(func.count(Listing.id)).filter(sold_scope).scalar() or 0
    written_off = db.query(func.count(Listing.id)).filter(written_off_scope).scalar() or 0

    # Count per platform
    platform_counts = (
        db.query(Listing.platform, func.count(Listing.id))
        .filter(total_scope)
        .group_by(Listing.platform)
        .all()
    )

    platforms = {p: c for p, c in platform_counts}

    # Total value (sum of active listings in scope)
    total_value = db.query(func.sum(Listing.price_cents)).filter(active_scope).scalar() or 0

    # Total investment / cost across active listings in scope
    total_cost = db.query(
        func.sum(Listing.purchase_price_cents + Listing.parts_cost_cents)
    ).filter(active_scope).scalar() or 0

    # Total profit realized from sold items in scope (revenue - costs)
    sold_revenue = db.query(func.sum(Listing.price_cents)).filter(sold_scope).scalar() or 0
    sold_cost = db.query(
        func.sum(Listing.purchase_price_cents + Listing.parts_cost_cents)
    ).filter(sold_scope).scalar() or 0
    sold_profit = sold_revenue - sold_cost

    # Written-off inventory loss cost
    written_off_cost = db.query(
        func.sum(Listing.purchase_price_cents + Listing.parts_cost_cents)
    ).filter(written_off_scope).scalar() or 0

    # Build timeline buckets for the chart
    from datetime import date
    now = datetime.utcnow()
    buckets = []
    bucket_map = {}

    # Query sold items in scope
    sold_rows = db.query(
        Listing.price_cents,
        Listing.purchase_price_cents,
        Listing.parts_cost_cents,
        coalesce(Listing.sold_at, Listing.updated_at).label("sold_date")
    ).filter(sold_scope).all()

    if days_int:
        # Daily buckets for 7 or 30 days
        for i in reversed(range(days_int)):
            d = (now - timedelta(days=i)).date()
            key = d.isoformat()
            label = d.strftime("%b %d")
            b = {
                "key": key,
                "label": label,
                "date_str": key,
                "revenue_cents": 0,
                "cost_cents": 0,
                "profit_cents": 0,
                "sold_count": 0,
            }
            buckets.append(b)
            bucket_map[key] = b
    else:
        # Monthly buckets for all time (at least 6 months up to current month)
        current_year = now.year
        current_month = now.month
        all_month_keys = set()
        for offset in reversed(range(6)):
            m = current_month - offset
            y = current_year
            while m <= 0:
                m += 12
                y -= 1
            all_month_keys.add(f"{y:04d}-{m:02d}")

        for row in sold_rows:
            dt = row.sold_date
            if isinstance(dt, str):
                try:
                    dt = datetime.fromisoformat(dt)
                except Exception:
                    continue
            if dt:
                all_month_keys.add(dt.strftime("%Y-%m"))

        min_key = min(all_month_keys)
        max_key = max(all_month_keys)
        min_y, min_m = int(min_key[:4]), int(min_key[5:7])
        max_y, max_m = int(max_key[:4]), int(max_key[5:7])

        cy, cm = min_y, min_m
        while (cy < max_y) or (cy == max_y and cm <= max_m):
            key = f"{cy:04d}-{cm:02d}"
            d = date(cy, cm, 1)
            b = {
                "key": key,
                "label": d.strftime("%b '%y"),
                "date_str": key,
                "revenue_cents": 0,
                "cost_cents": 0,
                "profit_cents": 0,
                "sold_count": 0,
            }
            buckets.append(b)
            bucket_map[key] = b
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1

    # Populate buckets from sold items
    for row in sold_rows:
        dt = row.sold_date
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                continue
        if not dt:
            continue

        b_key = dt.strftime("%Y-%m-%d") if days_int else dt.strftime("%Y-%m")
        if b_key in bucket_map:
            target_b = bucket_map[b_key]
            rev = row.price_cents or 0
            cost = (row.purchase_price_cents or 0) + (row.parts_cost_cents or 0)
            target_b["revenue_cents"] += rev
            target_b["cost_cents"] += cost
            target_b["profit_cents"] += (rev - cost)
            target_b["sold_count"] += 1

    timeline_points = [
        {
            "key": b["key"],
            "label": b["label"],
            "date": b["date_str"],
            "revenue_cents": b["revenue_cents"],
            "cost_cents": b["cost_cents"],
            "profit_cents": b["profit_cents"],
            "revenue": round(b["revenue_cents"] / 100.0, 2),
            "profit": round(b["profit_cents"] / 100.0, 2),
            "sold_count": b["sold_count"],
        }
        for b in buckets
    ]

    timeline = {
        "type": "daily" if days_int else "monthly",
        "period": f"{days_int}d" if days_int else "all",
        "points": timeline_points,
    }

    return {
        "period": f"{days_int}d" if days_int else "all",
        "days": days_int,
        "total_listings": total,
        "active_listings": active,
        "sold_listings": sold,
        "written_off_listings": written_off,
        "written_off_cost_cents": written_off_cost,
        "total_value_cents": total_value,
        "total_cost_cents": total_cost,
        "sold_revenue_cents": sold_revenue,
        "sold_cost_cents": sold_cost,
        "sold_profit_cents": sold_profit,
        "currency": config.DEFAULT_CURRENCY or "CAD",
        "currency_symbol": config.DEFAULT_CURRENCY_SYMBOL or "$",
        "platforms": platforms,
        "timeline": timeline,
    }

# -- Marketplace accounts (credentials) --

@api_router.get("/accounts")
async def get_accounts(db: Session = Depends(get_db), _admin: dict = Depends(check_admin)):
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
async def connect_account(body: dict, _admin: dict = Depends(check_admin)):
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
async def sync_account(body: dict, db: Session = Depends(get_db), _admin: dict = Depends(check_admin)):
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
    """The teams the current user belongs to, with each team's members and invite links."""
    teams = db.query(Team).filter(Team.id.in_(_user["team_ids"] or [0])).all()
    import secrets
    result = []
    for t in teams:
        if not t.invite_code:
            t.invite_code = secrets.token_urlsafe(16)
            db.commit()
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
            "invite_code": t.invite_code,
            "invite_url": f"{config.APP_BASE_URL}/join/{t.invite_code}",
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

    # Check duplicate team name within this user's teams
    user_team_names = {
        t.name.lower()
        for t in db.query(Team).join(TeamMembership, TeamMembership.team_id == Team.id)
        .filter(TeamMembership.user_id == _user["id"]).all()
    }
    if name.lower() in user_team_names:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"You already have a team named '{name}'"})

    import secrets
    invite_code = secrets.token_urlsafe(16)
    team = Team(name=name, invite_code=invite_code)
    db.add(team)
    db.flush()
    db.add(TeamMembership(team_id=team.id, user_id=_user["id"], role="owner"))
    db.commit()
    t_dict = team.to_dict()
    t_dict["invite_url"] = f"{config.APP_BASE_URL}/join/{team.invite_code}"
    return {"ok": True, "team": t_dict}


@api_router.post("/teams/{team_id}/members")
async def add_team_member(team_id: int, body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Add a member to a team by email (owner or admin only).

    If that person hasn't signed in yet, their user row is created up
    front — when they later sign in (Google) they land in the team
    automatically, so sharing just works.
    """
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})

    caller_m = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.user_id == _user["id"]
    ).first()
    is_owner = caller_m and caller_m.role == "owner"
    is_admin = bool(_user.get("is_admin") or _user.get("provider") == "local")
    if not (is_owner or is_admin):
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only team owners or administrators can invite new members"})

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
    team = db.get(Team, team_id)
    if not team.invite_code:
        import secrets
        team.invite_code = secrets.token_urlsafe(16)
    db.commit()

    invite_url = f"{config.APP_BASE_URL}/join/{team.invite_code}"
    inviter_name = _user.get("name") or "A team member"
    invite_subject = f"Invitation to join team '{team.name}' on {config.APP_NAME}"
    invite_body = (
        f"Hi,\n\n"
        f"{inviter_name} has invited you to collaborate on team '{team.name}' in {config.APP_NAME}.\n\n"
        f"Click the link below to accept the invitation and sign in:\n"
        f"{invite_url}\n\n"
        f"Welcome to the team!"
    )

    email_sent = False
    if config.SMTP_HOST and config.SMTP_USER:
        try:
            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = invite_subject
            msg["From"] = config.SMTP_FROM or config.SMTP_USER
            msg["To"] = email
            msg.set_content(invite_body)
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
                if config.SMTP_USE_TLS:
                    server.starttls()
                if config.SMTP_PASSWORD:
                    server.login(config.SMTP_USER, config.SMTP_PASSWORD)
                server.send_message(msg)
            email_sent = True
        except Exception:
            email_sent = False

    return {
        "ok": True,
        "email_sent": email_sent,
        "invite_url": invite_url,
        "invite_subject": invite_subject,
        "invite_body": invite_body,
        "team_name": team.name,
    }


@api_router.delete("/teams/{team_id}/members/{user_id}")
async def remove_team_member(team_id: int, user_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Remove a member from a team (must be team owner, the user leaving, or an administrator)."""
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
    membership = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        .first()
    )
    if not membership:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Member not found"})

    caller_membership = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.user_id == _user["id"]
    ).first()
    is_owner = caller_membership and caller_membership.role == "owner"
    is_self = _user["id"] == user_id
    is_admin = bool(_user.get("is_admin") or _user.get("provider") == "local")
    if not (is_owner or is_self or is_admin):
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only team owners or administrators can remove other members"})

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


@api_router.post("/teams/{team_id}/invite-code")
async def regenerate_invite_code(team_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Regenerate the shareable invite link for a team (owner only)."""
    if team_id not in _user["team_ids"]:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
    membership = db.query(TeamMembership).filter(
        TeamMembership.team_id == team_id,
        TeamMembership.user_id == _user["id"],
    ).first()
    if not membership or membership.role != "owner":
        return JSONResponse(status_code=403, content={"ok": False, "error": "Only team owners can regenerate invite links"})
    import secrets
    team = db.get(Team, team_id)
    team.invite_code = secrets.token_urlsafe(16)
    db.commit()
    return {
        "ok": True,
        "invite_code": team.invite_code,
        "invite_url": f"{config.APP_BASE_URL}/join/{team.invite_code}",
    }


@api_router.post("/teams/join")
async def join_team_api(body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Join a team via invite code."""
    code = (body.get("invite_code") or "").strip()
    if not code:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invite code is required"})
    team = db.query(Team).filter(Team.invite_code == code).first()
    if not team:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Invalid team invite code"})
    exists = db.query(TeamMembership).filter(
        TeamMembership.team_id == team.id,
        TeamMembership.user_id == _user["id"],
    ).first()
    if exists:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Already a member of this team"})
    db.add(TeamMembership(team_id=team.id, user_id=_user["id"], role="member"))
    db.commit()
    return {"ok": True, "team": team.to_dict()}


# -- Image uploads --

@api_router.post("/upload")
async def upload_image(file: UploadFile = File(...), _user: dict = Depends(check_auth)):
    """Upload an image file for a listing and store in static/uploads/."""
    import os
    import uuid

    if not file.filename:
        return JSONResponse(status_code=400, content={"ok": False, "error": "No file uploaded"})

    sanitized_filename = os.path.basename(file.filename)
    ext = os.path.splitext(sanitized_filename)[1].lower()
    allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if ext not in allowed_exts:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"Unsupported image extension '{ext}'. Allowed: {', '.join(sorted(allowed_exts))}"},
        )

    MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
    contents = await file.read(MAX_FILE_SIZE + 1)
    if len(contents) > MAX_FILE_SIZE:
        return JSONResponse(status_code=413, content={"ok": False, "error": "File size exceeds the 15 MB limit"})
    if len(contents) == 0:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Uploaded file is empty"})

    # Validate image header magic bytes to prevent polyglot / malicious payloads
    def is_valid_image(data: bytes, extension: str) -> bool:
        if extension in {".jpg", ".jpeg"}:
            return data.startswith(b"\xff\xd8\xff")
        if extension == ".png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        if extension == ".gif":
            return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")
        if extension == ".webp":
            return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        if extension == ".bmp":
            return data.startswith(b"BM")
        return False

    if not is_valid_image(contents, ext):
        return JSONResponse(status_code=400, content={"ok": False, "error": "File content does not match a valid image format"})

    uploads_dir = os.path.realpath(os.path.join("static", "uploads"))
    os.makedirs(uploads_dir, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
    dest_path = os.path.realpath(os.path.join(uploads_dir, safe_name))
    if not dest_path.startswith(uploads_dir + os.sep):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid file path"})
    with open(dest_path, "wb") as buffer:
        buffer.write(contents)

    return {"ok": True, "url": f"/static/uploads/{safe_name}"}


# -- Listings (creation, update, delete, local sales) --

@api_router.post("/listings/manual")
async def create_manual_listing(body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Create a manual listing or local sale entry directly in the dashboard."""
    import secrets
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Title is required"})

    price_cents = body.get("price_cents")
    if price_cents is None:
        try:
            price_val = float(body.get("price") or body.get("price_usd") or 0)
            price_cents = int(round(price_val * 100))
        except (ValueError, TypeError):
            price_cents = 0

    currency = (body.get("currency") or config.DEFAULT_CURRENCY or "CAD").upper().strip()

    # Platforms: array of cross-listed channels (e.g. ["ebay", "etsy", "facebook"])
    platforms_raw = body.get("platforms")
    cleaned_platforms = []
    if isinstance(platforms_raw, list):
        for p in platforms_raw:
            if isinstance(p, str) and p.strip():
                cleaned_platforms.append(p.strip().lower())
    if not cleaned_platforms:
        cleaned_platforms = [(body.get("platform") or "local").lower().strip()]
    platform = cleaned_platforms[0]

    status = (body.get("status") or "active").lower().strip()
    is_sold = status == "sold" or bool(body.get("is_sold"))
    quantity = int(body.get("quantity") or (0 if is_sold else 1))

    # Cost & Investment tracking (what we bought it for + parts/repairs)
    purchase_price_cents = body.get("purchase_price_cents")
    if purchase_price_cents is None:
        try:
            purchase_val = float(body.get("purchase_price") or body.get("cost") or 0)
            purchase_price_cents = int(round(purchase_val * 100))
        except (ValueError, TypeError):
            purchase_price_cents = 0

    parts_raw = body.get("parts") or []
    cleaned_parts = []
    parts_cost_cents = 0
    if isinstance(parts_raw, list):
        for p in parts_raw:
            if not isinstance(p, dict):
                continue
            desc = (p.get("description") or p.get("name") or "").strip()
            if not desc:
                continue
            c_cents = p.get("cost_cents")
            if c_cents is None:
                try:
                    c_cents = int(round(float(p.get("cost") or 0) * 100))
                except (ValueError, TypeError):
                    c_cents = 0
            else:
                c_cents = int(c_cents)
            cleaned_parts.append({
                "description": desc,
                "cost_cents": c_cents,
                "cost": round(c_cents / 100, 2),
            })
            parts_cost_cents += c_cents

    user_team_ids = _user.get("team_ids") or []
    if not user_team_ids:
        return JSONResponse(status_code=400, content={"ok": False, "error": "You must belong to a team to create listings"})

    team_id = body.get("team_id")
    if team_id:
        try:
            team_id = int(team_id)
            if team_id not in user_team_ids:
                return JSONResponse(status_code=404, content={"ok": False, "error": "Team not found"})
        except (ValueError, TypeError):
            team_id = None
    if not team_id:
        team_id = user_team_ids[0]

    sku = (body.get("sku") or "").strip()
    description = (body.get("description") or "").strip()
    category = (body.get("category") or "Local Sales").strip()
    image_url = (body.get("image_url") or "").strip() or "/static/img/placeholder.svg"

    platform_listing_id = sku or f"LOCAL-{secrets.token_hex(4).upper()}"

    listing = Listing(
        platform=platform,
        platforms_json=cleaned_platforms,
        platform_listing_id=platform_listing_id,
        title=title,
        description=description,
        price_cents=price_cents,
        price_raw=f"{currency} {price_cents / 100:.2f}",
        currency=currency,
        purchase_price_cents=purchase_price_cents,
        parts_cost_cents=parts_cost_cents,
        parts_json=cleaned_parts,
        status="sold" if is_sold else status,
        is_sold=is_sold,
        sold_at=datetime.utcnow() if is_sold else None,
        available_quantity=quantity,
        views_count=1,
        image_url=image_url,
        images_json=[image_url],
        category=category,
        sku=sku,
        team_id=team_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return {"ok": True, "listing": listing.to_dict()}


def _check_listing_access(listing, _user: dict, db: Session) -> bool:
    """Return True if caller has authorized access to this listing within their tenant teams."""
    if not listing:
        return False
    user_team_ids = _user.get("team_ids") or []
    # If unassigned legacy listing and caller is admin, claim it into admin team
    if not listing.team_id and (_user.get("is_admin") or _user.get("provider") == "local") and user_team_ids:
        listing.team_id = user_team_ids[0]
        db.commit()
    return bool(listing.team_id and listing.team_id in user_team_ids)


@api_router.post("/listings/{listing_id}/mark-sold")
async def mark_listing_sold(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Quickly mark a listing as sold (e.g. for a local in-person cash sale)."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    listing.status = "sold"
    listing.is_sold = True
    listing.sold_at = datetime.utcnow()
    listing.available_quantity = max(0, listing.available_quantity - 1)
    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}


@api_router.post("/listings/{listing_id}/write-off")
async def write_off_listing(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Mark a listing as written off (unsellable, damaged, obsolete inventory loss)."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    listing.status = "written_off"
    listing.is_sold = False
    listing.written_off_at = datetime.utcnow()
    listing.available_quantity = 0
    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}


@api_router.post("/listings/{listing_id}/restore")
async def restore_listing(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Restore a written-off or ended listing back to active status."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    listing.status = "active"
    listing.is_sold = False
    listing.written_off_at = None
    if listing.available_quantity <= 0:
        listing.available_quantity = 1
    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}


@api_router.delete("/listings/{listing_id}")
async def delete_listing(listing_id: int, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Delete a listing from the dashboard inventory."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})

    db.delete(listing)
    db.commit()
    return {"ok": True}


@api_router.put("/listings/{listing_id}")
async def update_listing(listing_id: int, body: dict, db: Session = Depends(get_db), _user: dict = Depends(check_auth)):
    """Update a listing (team assignment, purchase price, parts & repairs, price)."""
    listing = db.get(Listing, listing_id)
    if not _check_listing_access(listing, _user, db):
        return JSONResponse(status_code=404, content={"ok": False, "error": "Listing not found"})
    user_team_ids = _user.get("team_ids") or []

    if "team_id" in body:
        new_team = body["team_id"]
        if new_team is None:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Listings must be assigned to a valid team"})
        try:
            new_team = int(new_team)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"ok": False, "error": "team_id must be a number"})
        if new_team not in user_team_ids:
            return JSONResponse(status_code=404, content={"ok": False, "error": "Target team not found or unauthorized"})
        listing.team_id = new_team

    if "purchase_price" in body or "purchase_price_cents" in body:
        purchase_cents = body.get("purchase_price_cents")
        if purchase_cents is None:
            try:
                purchase_val = float(body.get("purchase_price") or 0)
                purchase_cents = int(round(purchase_val * 100))
            except (ValueError, TypeError):
                purchase_cents = 0
        listing.purchase_price_cents = max(0, int(purchase_cents))

    if "parts" in body:
        parts_raw = body.get("parts") or []
        cleaned_parts = []
        parts_cost_cents = 0
        if isinstance(parts_raw, list):
            for p in parts_raw:
                if not isinstance(p, dict):
                    continue
                desc = (p.get("description") or p.get("name") or "").strip()
                if not desc:
                    continue
                c_cents = p.get("cost_cents")
                if c_cents is None:
                    try:
                        c_cents = int(round(float(p.get("cost") or 0) * 100))
                    except (ValueError, TypeError):
                        c_cents = 0
                else:
                    c_cents = int(c_cents)
                cleaned_parts.append({
                    "description": desc,
                    "cost_cents": c_cents,
                    "cost": round(c_cents / 100, 2),
                })
                parts_cost_cents += c_cents
        listing.parts_cost_cents = parts_cost_cents
        listing.parts_json = cleaned_parts

    if "price" in body or "price_cents" in body:
        p_cents = body.get("price_cents")
        if p_cents is None:
            try:
                p_val = float(body.get("price") or 0)
                p_cents = int(round(p_val * 100))
            except (ValueError, TypeError):
                p_cents = listing.price_cents
        listing.price_cents = max(0, int(p_cents))
        curr = listing.currency or config.DEFAULT_CURRENCY or "CAD"
        listing.price_raw = f"{curr} {listing.price_cents / 100:.2f}"

    if "platforms" in body:
        platforms_raw = body.get("platforms") or []
        cleaned_platforms = []
        if isinstance(platforms_raw, list):
            for p in platforms_raw:
                if isinstance(p, str) and p.strip():
                    cleaned_platforms.append(p.strip().lower())
        listing.platforms_json = cleaned_platforms
        if cleaned_platforms:
            listing.platform = cleaned_platforms[0]

    if "image_url" in body:
        img_val = (body.get("image_url") or "").strip()
        listing.image_url = img_val or None

    if "status" in body:
        new_status = (body.get("status") or "").lower().strip()
        if new_status:
            listing.status = new_status
            if new_status == "sold":
                if not listing.is_sold:
                    listing.is_sold = True
                    listing.sold_at = datetime.utcnow()
            elif listing.is_sold:
                listing.is_sold = False
                listing.sold_at = None

    if "is_sold" in body:
        new_is_sold = bool(body["is_sold"])
        if new_is_sold and not listing.is_sold:
            listing.is_sold = True
            listing.sold_at = datetime.utcnow()
            listing.status = "sold"
        elif not new_is_sold and listing.is_sold:
            listing.is_sold = False
            listing.sold_at = None
            if listing.status == "sold":
                listing.status = "active"

    listing.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "listing": listing.to_dict()}

# -- Plugins info --

@api_router.get("/plugins")
async def get_plugins(_auth: bool = Depends(check_auth)):
    """Return metadata about all loaded plugins."""
    from src.plugins import get_loaded_plugins
    return get_loaded_plugins()
